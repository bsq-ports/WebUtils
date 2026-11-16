#include <span>
#include <WebUtils.hpp>
#include <RatelimitedDispatcher.hpp>
#include <android/log.h>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <functional>
#include <memory>
#include <stdexcept>

using namespace WebUtils;
using namespace std::chrono_literals;
static int passed = 0, failed = 0;
static FILE* output = stdout;
static void require(bool condition) { if (!condition) throw std::runtime_error("check failed"); }
static void check(const char* name, const std::function<void()>& body) {
    try { body(); ++passed; std::fprintf(output,"PASS %s\n", name); }
    catch (const std::exception& e) { ++failed; std::fprintf(output,"FAIL %s: %s\n", name, e.what()); }
    std::fflush(output);
}
static void ok(const StringResponse& r, std::string_view body) {
    if (!r.IsSuccessful() || !r.DataParsedSuccessful() || r.GetParsedData() != body)
        throw std::runtime_error("HTTP=" + std::to_string(r.HttpCode) + " curl=" + std::to_string(r.CurlStatus));
}
static std::span<const uint8_t> bytes(const std::string& s) {
    return {reinterpret_cast<const uint8_t*>(s.data()), s.size()};
}

int run_suite(int argc, char** argv) {
    if (argc != 7) return 2;
    const std::string http=argv[1], tls12=argv[2], tls13=argv[3], h2=argv[4], file=argv[5], ipv6=argv[6];
    DownloaderUtility d{.userAgent="WebUtils-network-tests", .timeOut=5};
    auto get = [&](const std::string& url) { return d.Get<StringResponse>(URLOptions(url)); };
    check("HTTP/1.1 GET and response headers", [&] {
        auto r=get(http+"/ok"); ok(r,"webutils-network");
        require(r.responseHeaders.find("HTTP/1.1 200")!=std::string::npos);
        require(r.responseHeaders.find("X-Test: present")!=std::string::npos);
    });
    check("raw binary response", [&] {
        auto r=d.Get<DataResponse>(URLOptions(http+"/binary"));
        require(r.IsSuccessful() && r.GetParsedData()==std::vector<uint8_t>({0,1,127,128,255}));
    });
    check("empty 204 response", [&] { auto r=get(http+"/empty"); ok(r,""); require(r.HttpCode==204); });
    check("GET redirect", [&] { ok(get(http+"/redirect"),"webutils-network"); });
    check("404 response", [&] { auto r=get(http+"/missing"); require(!r.IsSuccessful() && r.HttpCode==404 && r.CurlStatus==0); });
    check("500 response", [&] { auto r=get(http+"/error"); require(!r.IsSuccessful() && r.HttpCode==500); });
    check("query escaping and existing query/fragment", [&] {
        URLOptions u(http+"/inspect?old=yes#fragment", URLOptions::QueryMap{{"new key","a+b & /"}});
        auto r=d.Get<StringResponse>(u); require(r.IsSuccessful());
        require(r.GetParsedData().find("/inspect?old=yes&new%20key=a%2bb%20%26%20%2f\n") == 0);
    });
    check("preserve escaped paths", [&] { auto r=get(http+"/inspect/a%20b"); require(r.IsSuccessful() && r.GetParsedData().find("/inspect/a%20b\n")==0); });
    check("noEscape option", [&] {
        URLOptions u(http+"/inspect",URLOptions::QueryMap{{"raw","a+b"}}); u.noEscape=true;
        auto r=d.Get<StringResponse>(u); require(r.GetParsedData().find("/inspect?raw=a+b\n")==0);
    });
    check("custom headers and user agent", [&] {
        URLOptions u(http+"/inspect",{},URLOptions::HeaderMap{{"X-Custom","custom-value"}});
        u.userAgent="custom-agent"; auto r=d.Get<StringResponse>(u);
        require(r.IsSuccessful() && r.GetParsedData().find("custom-value\ncustom-agent")!=std::string::npos);
    });
    for (const char* encoding : {"gzip","deflate","br","zstd"}) check(encoding,[&] { ok(get(http+"/compress/"+encoding),"webutils-network"); });
    check("explicit compression selection", [&] { ok(d.Get<StringResponse>(URLOptions(http+"/compress/gzip",false,"gzip")),"webutils-network"); });
    const std::string payload("hello\0binary\xff",13);
    check("binary POST", [&] { ok(d.Post<StringResponse>(URLOptions(http+"/echo"),bytes(payload)),payload); });
    check("POST 307 retains method and body", [&] { ok(d.Post<StringResponse>(URLOptions(http+"/redirect307"),bytes(payload)),payload); });
    check("POST without response object", [&] { require(d.PostInto(URLOptions(http+"/echo"),bytes(payload),nullptr)); });
    check("null GET response", [&] { require(!d.GetInto(URLOptions(http+"/ok"),nullptr)); });
    check("async GET futures", [&] {
        std::vector<std::future<StringResponse>> fs;
        for(int i=0;i<8;++i) fs.push_back(d.GetAsync<StringResponse>(URLOptions(http+"/ok")));
        for(auto& f:fs) ok(f.get(),"webutils-network");
    });
    check("async POST future", [&] { ok(d.PostAsync<StringResponse>(URLOptions(http+"/echo"),bytes(payload)).get(),payload); });
    check("async GET callback", [&] {
        auto p=std::make_shared<std::promise<StringResponse>>(); auto f=p->get_future();
        d.GetAsync<StringResponse>(URLOptions(http+"/ok"),[p](StringResponse r){ p->set_value(std::move(r)); });
        require(f.wait_for(10s)==std::future_status::ready); ok(f.get(),"webutils-network");
    });
    check("async POST callback", [&] {
        auto p=std::make_shared<std::promise<StringResponse>>(); auto f=p->get_future();
        d.PostAsync<StringResponse>(URLOptions(http+"/echo"),bytes(payload),[p](StringResponse r){ p->set_value(std::move(r)); });
        require(f.wait_for(10s)==std::future_status::ready); ok(f.get(),payload);
    });
    check("async GetInto/PostInto", [&] {
        StringResponse a{},b{};
        require(d.GetAsyncInto(URLOptions(http+"/ok"),&a).get()); ok(a,"webutils-network");
        require(d.PostAsyncInto(URLOptions(http+"/echo"),bytes(payload),&b).get()); ok(b,payload);
    });
    check("download progress", [&] {
        int calls=0; bool valid=true; auto r=d.Get<DataResponse>(URLOptions(http+"/large"),[&](float p){++calls;valid &= std::isfinite(p)&&p>=0&&p<=1;});
        require(r.IsSuccessful() && r.GetParsedData().size()==262144 && calls>0 && valid);
    });
    check("upload progress", [&] {
        std::string data(262144,'x'); int calls=0;bool valid=true;
        auto r=d.Post<StringResponse>(URLOptions(http+"/echo"),bytes(data),[&](float p){++calls;valid &= std::isfinite(p)&&p>=0&&p<=1;});
        ok(r,data); require(calls>0&&valid);
    });
    check("per-request timeout", [&] {
        URLOptions u(http+"/slow");u.timeOut=1;auto start=std::chrono::steady_clock::now();auto r=d.Get<StringResponse>(u);
        require(r.CurlStatus==28 && !r.IsSuccessful() && std::chrono::steady_clock::now()-start<4s);
    });
    check("connection refusal", [&] { auto r=get("http://127.0.0.1:1/ok");require(r.CurlStatus==7&&!r.IsSuccessful()); });
    check("malformed URL", [&] { auto r=get("http://");require(r.CurlStatus!=0&&!r.IsSuccessful()); });
    check("TLS 1.2 HTTP/1.1 fallback", [&] { auto r=get(tls12+"/protocol");ok(r,"http/1.1"); });
    check("TLS 1.3 HTTP/1.1 fallback", [&] { auto r=get(tls13+"/protocol");ok(r,"http/1.1"); });
    check("reject untrusted TLS", [&] { auto r=d.Get<StringResponse>(URLOptions(h2+"/ok",true));require(r.CurlStatus==60&&!r.IsSuccessful()); });
    check("HTTP/2 GET negotiated by default", [&] { auto r=get(h2+"/protocol");ok(r,"h2");require(r.responseHeaders.find("HTTP/2 200")!=std::string::npos); });
    check("HTTP/2 POST negotiated by default", [&] { auto r=d.Post<StringResponse>(URLOptions(h2+"/echo"),bytes(payload));ok(r,payload);require(r.responseHeaders.find("HTTP/2 200")!=std::string::npos); });
    check("HTTP/2 concurrent requests", [&] {
        std::vector<std::future<StringResponse>> fs;for(int i=0;i<4;++i)fs.push_back(d.GetAsync<StringResponse>(URLOptions(h2+"/protocol")));
        for(auto& f:fs)ok(f.get(),"h2");
    });
    check("verified public HTTPS", [&] { auto r=d.Get<StringResponse>(URLOptions("https://example.com",true));require(r.IsSuccessful() && r.GetParsedData().find("Example Domain")!=std::string::npos); });
    if (ipv6!="-") check("IPv6 HTTP",[&]{ok(get(ipv6+"/ok"),"webutils-network");});
    check("file POST and GET", [&] {
        StringResponse written{};require(d.PostInto(URLOptions("file://"+file),bytes(payload),&written));
        auto r=get("file://"+file);ok(r,payload);
    });
    check("missing file", [&] { auto r=get("file://"+file+".missing");require(!r.IsSuccessful()&&r.HttpCode==404); });
    check("file write failure is reported", [&] { require(!d.PostInto(URLOptions("file://"+file+"/no-parent/file"),bytes(payload),nullptr)); });
    check("response parser failure", [&] {
        struct Reject : StringResponse { bool AcceptData(std::span<const uint8_t>) override {return false;} } r;
        require(!d.GetInto(URLOptions(http+"/ok"),&r));
    });
    check("rate limiting and retries", [&] {
        RatelimitedDispatcher dispatch;dispatch.downloader=d;dispatch.maxConcurrentRequests=2;dispatch.rateLimitTime=50ms;
        std::atomic<int> calls{0},successes{0};int finished=0;
        dispatch.onRequestFinished=[&](bool success,IRequest* request)->std::optional<RatelimitedDispatcher::RetryOptions>{
            ++calls;if(request->TargetResponse->HttpCode==429)return RatelimitedDispatcher::RetryOptions{25ms};
            if(success)++successes;return std::nullopt;
        };
        dispatch.allFinished=[&](auto requests){finished=requests.size();};
        for(int i=0;i<6;++i)dispatch.AddRequest<StringResponse>(URLOptions(http+"/ok"));
        dispatch.AddRequest<StringResponse>(URLOptions(http+"/retry"));
        auto start=std::chrono::steady_clock::now();dispatch.StartDispatchIfNeeded().get();
        require(finished==7&&successes==7&&calls==8&&!dispatch.AnyRequestsToDispatch());
        require(std::chrono::steady_clock::now()-start>=150ms);
        dispatch.AddRequest<StringResponse>(URLOptions(http+"/ok"));dispatch.StartDispatchIfNeeded().get();require(finished==1&&successes==8);
    });
    std::fprintf(output,"RESULT passed=%d failed=%d\n",passed,failed);std::fflush(output);
    return failed?1:0;
}

#ifndef WEBUTILS_GAME_PROBE
int main(int argc,char** argv) {return run_suite(argc,argv);}
#else
// Scotland2's small C metadata ABI; no loader implementation is linked.
struct CModInfo { const char* id; const char* version; uint64_t version_long; };
extern "C" __attribute__((visibility("default"))) void setup(CModInfo* info) {
    *info={"webutils-network-probe","1.0.0",0};
}
extern "C" __attribute__((visibility("default"))) void late_load() {
    std::thread([] {
        std::this_thread::sleep_for(5s);
        const std::string dir="/sdcard/ModData/com.beatgames.beatsaber/qpm-webutils-network-tests/";
        std::ifstream config(dir+"config.txt");std::vector<std::string> args{"probe"};std::string line;
        while(std::getline(config,line)) {
            if (!line.empty() && line.back()=='\r') line.pop_back();
            args.push_back(line);
        }
        std::vector<char*> raw;for(auto& a:args)raw.push_back(a.data());
        // A dedicated report avoids clearing or redirecting the game's logging.
        FILE* report=std::fopen((dir+"result.txt").c_str(),"w");
        if(!report){__android_log_print(ANDROID_LOG_ERROR,"WebUtilsProbe","Cannot open result");return;}
        output=report;
        int result=run_suite(raw.size(),raw.data());
        std::fclose(report);
        __android_log_print(result?ANDROID_LOG_ERROR:ANDROID_LOG_INFO,"WebUtilsProbe","RESULT passed=%d failed=%d",passed,failed);
    }).detach();
}
#endif

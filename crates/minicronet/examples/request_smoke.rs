use std::env;
use std::time::Duration;

use minicronet::{
    CacheMode, Engine, EngineConfig, ProtocolMode, RedirectMode, RequestConfig, Upload,
};

fn main() {
    let mut args = env::args().skip(1);
    let mode = args.next().unwrap_or_else(|| "success".to_owned());
    let url = args
        .next()
        .unwrap_or_else(|| "https://example.com/".to_owned());
    let engine_config = EngineConfig {
        protocol: match env::var("MINICRONET_PROTOCOL").as_deref() {
            Ok("h1") => ProtocolMode::ForceH1,
            Ok("h2") => ProtocolMode::ForceH2,
            Ok("h3") => ProtocolMode::ForceH3,
            Ok("native") | Err(_) => ProtocolMode::Native,
            Ok(other) => panic!("unsupported MINICRONET_PROTOCOL={other}"),
        },
        proxy_rules: env::var("MINICRONET_PROXY")
            .or_else(|_| env::var("MINICRONET_PROXY_RULES"))
            .ok(),
        proxy_username: env::var("MINICRONET_PROXY_USERNAME").ok(),
        proxy_password: env::var("MINICRONET_PROXY_PASSWORD").ok(),
        accept_language: Some("en-US,en;q=0.9".into()),
        http_cache: if mode == "cache-disabled" {
            minicronet::HttpCacheMode::Disabled
        } else {
            minicronet::HttpCacheMode::Enabled
        },
        ..EngineConfig::default()
    };
    let engine = Engine::new(engine_config).expect("engine");
    if mode == "concurrent" {
        let count = args
            .next()
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(1);
        let workers = (0..count)
            .map(|_| {
                let engine = engine.clone();
                let url = url.clone();
                std::thread::spawn(move || {
                    let request = engine.request(RequestConfig::get(url)).expect("request");
                    let response = request.start().expect("start").wait().expect("response");
                    drain(response.body);
                })
            })
            .collect::<Vec<_>>();
        for worker in workers {
            worker.join().expect("worker");
        }
        println!("concurrent_requests={count}");
        return;
    }
    if mode == "cookie" {
        let set = engine
            .request(RequestConfig::get(format!("{url}/cookie-set")))
            .expect("cookie set");
        let response = set
            .start()
            .expect("cookie set start")
            .wait()
            .expect("cookie set response");
        drain(response.body);
        let read = engine
            .request(RequestConfig::get(format!("{url}/cookie-read")))
            .expect("cookie read");
        let response = read
            .start()
            .expect("cookie read start")
            .wait()
            .expect("cookie read response");
        let body = collect(response.body);
        assert!(String::from_utf8_lossy(&body).contains("mn_cookie=1"));
        println!("cookie preserved");
        return;
    }
    if mode == "cache-modes" {
        for cache in [
            CacheMode::Default,
            CacheMode::Validate,
            CacheMode::Bypass,
            CacheMode::NoStore,
        ] {
            let mut config = RequestConfig::get(url.clone());
            config.cache = cache;
            let request = engine.request(config).expect("cache request");
            let response = request
                .start()
                .expect("cache start")
                .wait()
                .expect("cache response");
            drain(response.body);
        }
        println!("cache_modes=4");
        return;
    }
    let mut request_config = RequestConfig::get(url);
    request_config.timeout = (mode == "timeout").then_some(std::time::Duration::from_millis(50));
    if mode == "redirect-manual" {
        request_config.redirect = RedirectMode::Manual;
    } else if mode == "redirect-error" {
        request_config.redirect = RedirectMode::Error;
    } else if mode == "cache-miss" {
        request_config.cache = CacheMode::OnlyIfCached;
    }
    if mode == "redirect-upload" {
        request_config.method = "PUT".into();
        request_config.upload = Upload::Fixed(b"redirect-body".to_vec());
    } else if mode == "method" || mode == "chunked" {
        request_config.method = args.next().unwrap_or_else(|| "POST".to_owned());
        let body = args.next().unwrap_or_else(|| "payload".to_owned());
        request_config.upload = if mode == "chunked" {
            Upload::Chunked
        } else {
            Upload::Fixed(body.into_bytes())
        };
    }
    let request = engine.request(request_config).expect("request");
    let future = request.start().expect("start");
    assert!(request.start().is_err(), "a request must start only once");
    if mode == "cancel" {
        request.cancel().expect("cancel");
    } else if mode == "chunked" {
        request.upload_write(b"chunked-", false).expect("upload");
        request.upload_finish().expect("upload finish");
    }
    if mode == "redirect-manual" {
        let redirect = request
            .wait_for_redirect(Duration::from_secs(5))
            .expect("redirect wait")
            .expect("redirect");
        assert_eq!(redirect.status_code, 302);
        request.follow_redirect().expect("follow redirect");
    }
    let response = match future.wait() {
        Ok(response)
            if matches!(
                mode.as_str(),
                "success"
                    | "dump"
                    | "method"
                    | "chunked"
                    | "cache-disabled"
                    | "cache-modes"
                    | "redirect-manual"
                    | "redirect-upload"
                    | "concurrent"
            ) =>
        {
            response
        }
        Err(_)
            if matches!(
                mode.as_str(),
                "network-error" | "timeout" | "cancel" | "cache-miss" | "redirect-error"
            ) =>
        {
            return
        }
        Err(error) => panic!("request failed: {error}"),
        Ok(_) => panic!("request unexpectedly succeeded"),
    };
    println!(
        "status={} headers={}",
        response.status_code,
        response.headers.len()
    );
    if mode == "dump" {
        println!("{}", String::from_utf8_lossy(&response.headers));
    }
    let body_bytes = collect(response.body);
    let total = body_bytes.len();
    println!("body_bytes={total}");
    if mode == "dump" {
        println!("{}", String::from_utf8_lossy(&body_bytes));
    }
}

fn collect(mut body: minicronet::ResponseStream) -> Vec<u8> {
    let mut bytes = Vec::new();
    while let Some(chunk) = body.blocking_next() {
        bytes.extend_from_slice(&chunk.expect("body"));
    }
    bytes
}

fn drain(body: minicronet::ResponseStream) {
    let _ = collect(body);
}

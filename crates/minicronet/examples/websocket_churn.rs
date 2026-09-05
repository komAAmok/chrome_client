//! Isolates the WebSocket retention measurement to the Rust + Core layers.
//!
//! Python is not involved, so a linear RSS growth here proves the leak is not in
//! the binding's Python objects.

use std::env;
use std::time::Duration;

use minicronet::{Engine, EngineConfig, WebSocketConfig, WebSocketEvent};

fn rss_kib() -> i64 {
    let statm = std::fs::read_to_string("/proc/self/statm").unwrap_or_default();
    let pages: i64 = statm
        .split_whitespace()
        .nth(1)
        .and_then(|value| value.parse().ok())
        .unwrap_or(0);
    pages * 4
}

fn connect(engine: &Engine, url: &str) {
    let socket = engine
        .websocket(WebSocketConfig::new(url, origin_of(url)))
        .expect("create");
    let mut events = socket.start().expect("start");
    // Read until the open event plus the server's first frame.
    let mut frames = 0;
    while let Some(event) = events.blocking_next() {
        match event {
            WebSocketEvent::Open { .. } => {}
            WebSocketEvent::Message { .. } => {
                frames += 1;
                break;
            }
            WebSocketEvent::Closed(_) | WebSocketEvent::Failure(_) => break,
            WebSocketEvent::Closing => {}
        }
    }
    assert!(frames <= 1);
    let _ = socket.close(1000, "");
    // Drain the terminal event so the Core's callback reference is released.
    while let Some(event) = events.blocking_next() {
        if matches!(
            event,
            WebSocketEvent::Closed(_) | WebSocketEvent::Failure(_)
        ) {
            break;
        }
    }
}

fn origin_of(url: &str) -> String {
    let rest = url
        .strip_prefix("ws://")
        .map(|value| ("http://", value))
        .or_else(|| url.strip_prefix("wss://").map(|value| ("https://", value)));
    match rest {
        Some((scheme, remainder)) => {
            let host = remainder.split('/').next().unwrap_or(remainder);
            format!("{scheme}{host}")
        }
        None => url.to_owned(),
    }
}

fn main() {
    let url = env::args()
        .nth(1)
        .unwrap_or_else(|| "ws://127.0.0.1:8765/".to_owned());
    let engine = Engine::new(EngineConfig::default()).expect("engine");
    for _ in 0..50 {
        connect(&engine, &url);
    }
    std::thread::sleep(Duration::from_millis(500));
    let base = rss_kib();
    for round in 0..4 {
        for _ in 0..200 {
            connect(&engine, &url);
        }
        std::thread::sleep(Duration::from_millis(500));
        println!("rust round {round}: RSS delta {:+} KiB", rss_kib() - base);
    }
}

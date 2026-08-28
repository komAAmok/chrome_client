use std::{env, fs};

use minicronet::{
    Engine, EngineConfig, Header, MessageType, ProtocolMode, TlsVerifyMode, WebSocketConfig,
    WebSocketEvent,
};

fn main() {
    let mut args = env::args().skip(1);
    let mode = args.next().unwrap_or_else(|| "success".to_owned());
    let url = args
        .next()
        .unwrap_or_else(|| "ws://127.0.0.1:8765/echo".to_owned());
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
        tls_verify: env::var_os("MINICRONET_CUSTOM_CA")
            .map(|_| TlsVerifyMode::CustomCa)
            .unwrap_or(TlsVerifyMode::ChromiumDefault),
        custom_ca_pem: env::var_os("MINICRONET_CUSTOM_CA")
            .map(fs::read)
            .transpose()
            .expect("CA"),
        ..EngineConfig::default()
    };
    let engine = Engine::new(engine_config).expect("engine");
    let mut config = WebSocketConfig::new(url, "https://example.test");
    if mode == "timeout" {
        config.timeout = Some(std::time::Duration::from_millis(300));
    }
    config.protocols.push("chat".into());
    config.headers.push(Header::new("X-Test", "minicronet"));
    let websocket = engine.websocket(config).expect("websocket");
    let mut events = websocket.start().expect("start");
    if mode == "cancel" {
        websocket.cancel().expect("cancel");
    }
    let mut sent = false;
    let mut received = 0;
    while let Some(event) = events.blocking_next() {
        match event {
            WebSocketEvent::Open { .. } if !sent => {
                websocket.send_text("client-text").expect("text");
                websocket.send_binary(&[0, 1, 2, 255]).expect("binary");
                sent = true;
            }
            WebSocketEvent::Message {
                message_type: MessageType::Text | MessageType::Binary,
                ..
            } => {
                received += 1;
                if received == 2 {
                    websocket.close(1000, "done").expect("close");
                }
            }
            WebSocketEvent::Closed(info) => {
                if env::var_os("MINICRONET_ALLOW_UNCLEAN_CLOSE").is_none() {
                    assert_eq!(info.code, 1000);
                }
                println!("closed code={}", info.code);
                return;
            }
            WebSocketEvent::Failure(_failure) if mode != "success" => return,
            WebSocketEvent::Failure(failure) => panic!("websocket failure: {failure}"),
            _ => {}
        }
    }
    panic!("websocket ended without a close event");
}

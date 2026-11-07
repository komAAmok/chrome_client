// One request under a named profile, printing the complete response body.
//
// This is the driver behind tools/run-fingerprint-alignment.py: it exists so the
// 6-connection logs are produced by the same public Rust API an application
// uses, with the profile selected exactly the way a caller selects it. It prints
// the raw body rather than a digest because the acceptance evidence has to be
// the complete response, not a summary of it.
//
//   MINICRONET_PROTOCOL=native|h1|h2|h3 \
//     fingerprint_probe chrome_86 https://tls.peet.ws/api/all
use minicronet::{Engine, EngineConfig, ProtocolMode, RequestConfig};
use std::env;

fn main() {
    let profile = env::args().nth(1).expect("profile id, e.g. chrome_86");
    let url = env::args()
        .nth(2)
        .unwrap_or_else(|| "https://tls.peet.ws/api/all".into());
    let protocol = match env::var("MINICRONET_PROTOCOL").as_deref() {
        Ok("h1") => ProtocolMode::ForceH1,
        Ok("h2") => ProtocolMode::ForceH2,
        Ok("h3") => ProtocolMode::ForceH3,
        _ => ProtocolMode::Native,
    };
    let engine = Engine::new(EngineConfig {
        profile_id: Some(profile.clone()),
        profile_namespace: Some(format!("minicronet/{profile}")),
        protocol,
        ..EngineConfig::default()
    })
    .expect("engine");
    let request = engine.request(RequestConfig::get(url)).expect("request");
    let response = request.start().expect("start").wait().expect("response");
    let mut body = Vec::new();
    let mut stream = response.body;
    while let Some(chunk) = stream.blocking_next() {
        body.extend_from_slice(&chunk.expect("body"));
    }
    println!("status={}", response.status_code);
    println!("{}", String::from_utf8_lossy(&body));
}

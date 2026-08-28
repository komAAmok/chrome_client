use std::{env, path::PathBuf};

fn target_directory(target: &str) -> Option<&'static str> {
    match target {
        "i686-unknown-linux-gnu" => Some("linux-x86"),
        "x86_64-unknown-linux-gnu" => Some("linux-x86_64"),
        "aarch64-unknown-linux-gnu" => Some("linux-arm64"),
        "i686-pc-windows-msvc" => Some("windows-x86"),
        "x86_64-pc-windows-msvc" => Some("windows-x86_64"),
        "aarch64-pc-windows-msvc" => Some("windows-arm64"),
        "x86_64-apple-darwin" => Some("macos-x86_64"),
        "aarch64-apple-darwin" => Some("macos-arm64"),
        _ => None,
    }
}

fn main() {
    let manifest_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let repository = manifest_dir.parent().unwrap().parent().unwrap();
    println!(
        "cargo:rerun-if-changed={}",
        repository.join("core/abi/minicronet.h").display()
    );
    let target = env::var("TARGET").unwrap();
    let target_name = target_directory(&target);
    let default_dir = target_name.map(|name| repository.join("core/binaries").join(name));
    let core_dir = env::var_os("MINICRONET_CORE_DIR")
        .map(PathBuf::from)
        .or(default_dir);

    let Some(core_dir) = core_dir else {
        println!("cargo:warning=unsupported target {target}; native Core linking is disabled");
        return;
    };

    let library_file = if target.contains("windows") {
        core_dir.join("minicronet.lib")
    } else if target.contains("apple") {
        core_dir.join("libminicronet.dylib")
    } else {
        core_dir.join("libminicronet.so")
    };
    let runtime_file = if target.contains("windows") {
        core_dir.join("minicronet.dll")
    } else if target.contains("apple") {
        core_dir.join("libminicronet.dylib")
    } else {
        core_dir.join("libminicronet.so")
    };

    if !library_file.exists() {
        if env::var_os("MINICRONET_REQUIRE_NATIVE").is_some() {
            panic!(
                "MiniCronet Core not found for {target}: {}",
                library_file.display()
            );
        }
        println!(
            "cargo:warning=MiniCronet Core not found for {target}; native linking is disabled"
        );
        return;
    }
    if env::var_os("MINICRONET_REQUIRE_NATIVE").is_some() && !runtime_file.exists() {
        panic!(
            "MiniCronet runtime binary not found for {target}: {}",
            runtime_file.display()
        );
    }

    println!("cargo:rustc-link-search=native={}", core_dir.display());
    println!("cargo:rustc-link-lib=dylib=minicronet");
    println!("cargo:rerun-if-changed={}", library_file.display());
    println!("cargo:rerun-if-changed={}", runtime_file.display());
    println!("cargo:rerun-if-env-changed=MINICRONET_CORE_DIR");
    println!("cargo:rerun-if-env-changed=MINICRONET_REQUIRE_NATIVE");
}

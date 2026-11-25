// Copyright 2026 The chrome_client Authors.
// One table for the two strings that state a browser build identity.
//
// The User-Agent group and sec-ch-ua-platform are both derived from
// mn_platform_t, and both are published through the ABI
// (mn_platform_user_agent_group / mn_platform_hint) and used internally
// (DefaultUserAgent, Engine::platform_hint). Keeping the switch in one place is
// the only way the two spellings of "this build is Linux" cannot drift apart:
// AGENTS section 1.1 asks whether a second implementation would have to be
// written again in another language, and a second copy in the same language is
// worse.

#ifndef MINICRONET_PLATFORM_IDENTITY_H_
#define MINICRONET_PLATFORM_IDENTITY_H_

#include "build/build_config.h"
#include "build/buildflag.h"
#include "minicronet.h"

namespace minicronet {

// The User-Agent parenthesised group, without the parentheses.
inline const char *PlatformToken(mn_platform_t platform) {
  switch (platform) {
  case MN_PLATFORM_WINDOWS:
    return "Windows NT 10.0; Win64; x64";
  case MN_PLATFORM_MACOS:
    return "Macintosh; Intel Mac OS X 10_15_7";
  case MN_PLATFORM_LINUX:
    // Which Linux token is decided by the architecture this Core was built for,
    // exactly as a real Chrome build decides it. The override selects the
    // family, not a fabricated machine type.
#if defined(ARCH_CPU_ARM64)
    return "X11; Linux aarch64";
#elif defined(ARCH_CPU_X86)
    return "X11; Linux i686";
#else
    return "X11; Linux x86_64";
#endif
  case MN_PLATFORM_AUTO:
    break;
  }
#if BUILDFLAG(IS_WIN)
  return "Windows NT 10.0; Win64; x64";
#elif BUILDFLAG(IS_MAC)
  return "Macintosh; Intel Mac OS X 10_15_7";
#elif defined(ARCH_CPU_ARM64)
  return "X11; Linux aarch64";
#elif defined(ARCH_CPU_X86)
  return "X11; Linux i686";
#else
  return "X11; Linux x86_64";
#endif
}

// The sec-ch-ua-platform value, quotes included, e.g. "Linux".
inline const char *PlatformHint(mn_platform_t platform) {
  switch (platform) {
  case MN_PLATFORM_WINDOWS:
    return "\"Windows\"";
  case MN_PLATFORM_MACOS:
    return "\"macOS\"";
  case MN_PLATFORM_LINUX:
    return "\"Linux\"";
  case MN_PLATFORM_AUTO:
    break;
  }
#if BUILDFLAG(IS_WIN)
  return "\"Windows\"";
#elif BUILDFLAG(IS_MAC)
  return "\"macOS\"";
#else
  return "\"Linux\"";
#endif
}

}  // namespace minicronet

#endif  // MINICRONET_PLATFORM_IDENTITY_H_
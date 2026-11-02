# SmartVoice Android releases

Current release: **0.2.0-beta.1 / versionCode 10**.

[Download the APK](https://github.com/iamnevillechen-max/smartvoice-android-releases/releases/download/v0.2.0-beta.1/SmartVoice-0.2.0-beta.1-release.apk)

SmartVoice is an Android system text-to-speech engine. This APK contains 36 original Vocalizer voices. English uses Samantha; an unstable Thai voice has been removed. The original engine runs through an ARM32 compatibility layer on the ARM64 host.

The Voices page groups engines horizontally. Optional Kokoro Chinese and English voices use a native ARM64 backend and can be downloaded inside the app. The interface supports 简体中文, 繁體中文 and English. The global multiplier applies to system TTS and voice previews.

`update.json` is the app update manifest. `voices-v2.json` is the current multi-engine catalogue; `voices.json` is retained for older app versions. All APK and voice package downloads have pinned sizes and SHA-256 digests.

This release was tested automatically on an isolated Android 17 ARM64 / 4 KiB emulator. It has not been accepted on the user's physical phone in this round, and automated PCM/playback checks do not replace human listening or TalkBack testing. Kokoro prioritizes voice quality; its native cancellation still waits for a synthesis callback.

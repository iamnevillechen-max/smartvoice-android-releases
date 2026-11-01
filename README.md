# SmartVoice 中文版

Android 系統 TTS 引擎，含原有45個音色。中文介面為音色、語言分配及設定，支援全局語速倍數、音色包下載、線上與本地APK更新。

[直接下載0.1.1-beta.4 APK（80.12MB）](https://github.com/iamnevillechen-max/smartvoice-android-releases/releases/download/v0.1.1-beta.4/SmartVoice-0.1.1-beta.4-release.apk)

本版是安裝相容測試：調整名稱／主題資源、加入普通PNG圖示，原生庫明確採解壓安裝。Android17 AOSP 已通過套件解析、系統確認安裝、45音色及8組長短句；REDMI Turbo 5 Max／HyperOS4的原有解析(33)問題，仍待實機確認是否解決。

最低Android8，ARM64，目標Android17。使用者聆聽／TalkBack及16KB實際運行仍未驗收。

`update.json`及`voices.json`為App的穩定目錄；APK及音色放在固定版本Release assets。此庫不存放簽署私鑰。

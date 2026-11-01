# SmartVoice

Android系統TTS引擎，中文介面，底部分頁「音色／語言分配／設定」，隨附45個原音色、37種語言／地區。支援全局語速倍數、線上檢查更新、音色下載及本地APK更新。

[直接下載SmartVoice 0.1.1-beta.6 APK](https://github.com/iamnevillechen-max/smartvoice-android-releases/releases/download/v0.1.1-beta.6/SmartVoice-0.1.1-beta.6-release.apk)

versionCode9，80,122,083 bytes；SHA-256 `20c902084e052aad579b51900bbac5589f78808992d28687b35fb120d6184783`。最低Android8，目標Android17，ARM64宿主透過相容層執行未修改的原ARM32核心。

完整APK已在REDMI Turbo5Max／Android17安裝：45音色驗證約19秒、冷啟語音準備約2.6秒，8組系統TTS及App內試聽播放通過；8組PCM與原核心基準逐位元相同。人耳聆聽及TalkBack操作另行驗收。

先前 `(33) packageInfo is null` 已確認為手機下載副本內有零填充損壞；此完整APK已通過該機小米安裝器簽章解析。

App讀取本庫main/update.json及main/voices.json，更新沿用同一專案簽署。

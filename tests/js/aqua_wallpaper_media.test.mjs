import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const aquaSource = await readFile(
  new URL("../../harness/plugins/dsh-client-ui-aqua/lib/client.js", import.meta.url),
  "utf8",
);

test("Aqua 壁纸图片和视频都使用嵌入式浏览器兼容的文件输入", () => {
  assert.match(aquaSource, /const WALLPAPER_IMAGE_ACCEPT = .*image\/jpeg/);
  assert.match(aquaSource, /const WALLPAPER_VIDEO_ACCEPT = .*video\/quicktime/);
  assert.match(aquaSource, /fileRef\.current\?\.click\(\)/);
  assert.match(aquaSource, /videoRef\.current\?\.click\(\)/);
  assert.doesNotMatch(aquaSource, /showOpenFilePicker/);
});

test("Aqua 壁纸媒体在应用前校验格式、大小和浏览器解码能力", () => {
  assert.match(aquaSource, /MAX_WALLPAPER_IMAGE_BYTES = 30 \* 1024 \* 1024/);
  assert.match(aquaSource, /MAX_WALLPAPER_VIDEO_BYTES = 200 \* 1024 \* 1024/);
  assert.match(aquaSource, /validateWallpaperFile\(file, "image"\)/);
  assert.match(aquaSource, /validateWallpaperFile\(file, "video"\)/);
  assert.match(aquaSource, /await probeWallpaperVideo\(file\)/);
  assert.match(aquaSource, /VIDEO_STORAGE_FAILED/);
  assert.doesNotMatch(aquaSource, /else fileToDataUrl\(file\)\.then\(setWallpaper\)/);
});

test("Aqua 壁纸上传显示处理中、成功和失败反馈", () => {
  assert.match(aquaSource, /aqua\.mediaProcessing/);
  assert.match(aquaSource, /aqua\.imageApplied/);
  assert.match(aquaSource, /aqua\.videoApplied/);
  assert.match(aquaSource, /role: mediaFeedback\.tone === "error" \? "alert" : "status"/);
  assert.match(aquaSource, /"aria-live": "polite"/);
});

test("Aqua 空壁纸不会显示无 src 的全屏破图层", () => {
  assert.match(aquaSource, /wallpaper\.hidden = true/);
  assert.match(aquaSource, /data-dsh-aqua-wallpaper-img alt=\\"\\" hidden/);
  assert.match(aquaSource, /const wallpaperOn = this\.settings\.background === "wallpaper" && wallpaper !== ""/);
  assert.match(aquaSource, /const visualBackground = wallpaperVisible \? "wallpaper" : "fluid"/);
  assert.match(aquaSource, /wallpaperLayer\.hidden = !wallpaperVisible/);
  assert.match(aquaSource, /ambient\.dataset\.background = visualBackground/);
  assert.match(aquaSource, /img\.hidden = !wallpaperVisible \|\| isVideo/);
  assert.match(aquaSource, /video\.hidden = !wallpaperVisible \|\| !isVideo/);
});

test("Aqua 视频壁纸在首次播放前静音并启用循环内联播放", () => {
  const configureStart = aquaSource.indexOf("configureWallpaperVideo(video) {");
  const configureEnd = aquaSource.indexOf("\n\t\t\t}\n", configureStart);
  const configureSource = aquaSource.slice(configureStart, configureEnd);
  assert.ok(configureStart >= 0);
  assert.match(configureSource, /video\.loop = true/);
  assert.match(configureSource, /video\.muted = true/);
  assert.match(configureSource, /video\.defaultMuted = true/);
  assert.match(configureSource, /video\.autoplay = shouldPlay/);
  assert.match(configureSource, /video\.playsInline = true/);
  assert.ok(configureSource.indexOf("video.muted = true") < configureSource.indexOf("video.play()"));
});

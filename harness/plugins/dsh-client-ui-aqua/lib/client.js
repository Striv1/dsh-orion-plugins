window.__ModuleLoader__.load({
	id: "dsh-client-ui-aqua",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let _deepseek_ai_dsh_client_ui_primitives = require("@deepseek-ai/dsh-client-ui-primitives");
		// 0.2 names icons by artwork weight; older runtimes use fixed-size names.
		const AquaCheckIcon = _deepseek_ai_dsh_client_ui_primitives.IconCheckOutlineRegular ?? _deepseek_ai_dsh_client_ui_primitives.IconCheckOutline16;
		let react_jsx_runtime = require("react/jsx-runtime");
		let react = require("react");
		let _deepseek_ai_dsh_client_store = require("@deepseek-ai/dsh-client-store");
		//#region \0dsh-css:D:\Hermes Work\deepseek-harness\packages\client\ui-aqua\src\client\AquaPluginCard.module.css.mjs
		const css$3 = ".EG3s1W_tab{margin:0;padding:0;display:grid;gap:12px}.EG3s1W_card{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-1);border-radius:12px;flex-direction:column;padding:16px;display:flex}.EG3s1W_head{justify-content:space-between;align-items:center;gap:16px;display:flex}.EG3s1W_text{flex-direction:column;gap:2px;min-width:0;display:flex}.EG3s1W_title{color:var(--dsw-alias-label-primary);font-size:14px;font-weight:500;line-height:22px}.EG3s1W_description{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:18px}.EG3s1W_toggle{border:1px solid var(--dsw-alias-border-l2);height:28px;color:var(--dsw-alias-label-primary);cursor:pointer;background:0 0;border-radius:14px;flex:none;align-items:center;gap:6px;padding:0 10px 0 6px;font-size:12px;line-height:18px;display:inline-flex}.EG3s1W_toggle:hover{background:var(--dsw-alias-interactive-bg-hover)}.EG3s1W_toggle[aria-pressed=true]{background:var(--dsw-alias-state-business-tertiary);color:var(--dsw-alias-state-business-primary);border-color:#0000}.EG3s1W_check{justify-content:center;align-items:center;width:16px;height:16px;display:inline-flex}";
		const tagId$3 = "dsh-client-ui-aqua/AquaPluginCard.module.css";
		if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId$3) + "]") === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = "dsh-client-ui-aqua";
			tag.dataset.pluginCss = tagId$3;
			tag.textContent = css$3;
			document.head.appendChild(tag);
		}
		var AquaPluginCard_module_css_default = {
			"tab": "EG3s1W_tab",
			"check": "EG3s1W_check",
			"card": "EG3s1W_card",
			"head": "EG3s1W_head",
			"title": "EG3s1W_title",
			"description": "EG3s1W_description",
			"toggle": "EG3s1W_toggle",
			"text": "EG3s1W_text"
		};
		//#endregion
		//#region src/client/AquaPluginCard.tsx
		/**
		* Aqua card registered as its own Plugins settings tab
		* (`settings.plugins.tab`): the master on/off switch — name, description, and
		* one toggle, in the section's card language. Every other knob lives in the
		* General settings' Appearance row, so the card stays the same shape as the
		* other plugin cards.
		*/
		/**
		* Render the Aqua plugin card.
		* @param props - composed slot props.
		* @returns the card list item.
		*/
		function AquaPluginCard(props) {
			const { t, setEnabled, useStore } = props;
			const enabled = useStore((s) => s.enabled);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("section", {
				className: AquaPluginCard_module_css_default.card,
				children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: AquaPluginCard_module_css_default.head,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: AquaPluginCard_module_css_default.text,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: AquaPluginCard_module_css_default.title,
							children: t("aqua.title")
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: AquaPluginCard_module_css_default.description,
							children: t("aqua.description")
						})]
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
						type: "button",
						className: AquaPluginCard_module_css_default.toggle,
						"aria-pressed": enabled,
						onClick: () => {
							setEnabled(!enabled);
						},
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: AquaPluginCard_module_css_default.check,
							children: enabled && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(AquaCheckIcon, { size: 16 })
						}), enabled ? t("aqua.enable") : t("aqua.disable")]
					})]
				})
			});
		}
		/** Render the standalone Alpha Plugins tab while preserving card-list semantics. */
		function AquaPluginTab(props) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: AquaPluginCard_module_css_default.tab,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(AquaPluginCard, { ...props }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(AquaAppearanceRow, { ...props })]
			});
		}
		//#endregion
		//#region \0dsh-css:D:\Hermes Work\deepseek-harness\packages\client\ui-aqua\src\client\AquaAppearanceRow.module.css.mjs
		const css$2 = ".VYJBRq_group{border-bottom:1px solid var(--dsw-alias-border-l2);flex-direction:column;gap:14px;padding:8px 0 16px;display:flex}.VYJBRq_subGroup{flex-direction:column;gap:8px;display:flex}.VYJBRq_subTitle{color:var(--dsw-alias-label-primary);font-size:13px;font-weight:600;line-height:20px}.VYJBRq_controls{flex-direction:column;gap:10px;display:flex}.VYJBRq_row{align-items:center;gap:10px;display:flex}.VYJBRq_rowLabel{width:92px;color:var(--dsw-alias-label-secondary);flex:none;font-size:12px;line-height:18px}.VYJBRq_inlineLabel{color:var(--dsw-alias-label-secondary);flex:none;font-size:12px;line-height:18px}.VYJBRq_rowHint{color:var(--dsw-alias-label-tertiary);margin-top:-4px;margin-left:102px;font-size:12px;line-height:18px}.VYJBRq_groupHint{color:var(--dsw-alias-label-tertiary);margin-top:-4px;font-size:12px;line-height:18px}.VYJBRq_knobHint{color:var(--dsw-alias-label-tertiary);margin-top:-4px;margin-left:102px;font-size:12px;line-height:18px}.VYJBRq_mediaStatus{margin:-4px 0 0 102px;color:var(--dsw-alias-label-secondary);font-size:12px;line-height:18px}.VYJBRq_mediaStatus[data-tone=error]{color:var(--dsw-alias-label-error)}.VYJBRq_mediaStatus[data-tone=success]{color:var(--dsw-alias-state-success-primary)}.VYJBRq_toggle,.VYJBRq_toggleOn{border:1px solid var(--dsw-alias-border-l2);height:28px;color:var(--dsw-alias-label-primary);cursor:pointer;background:0 0;border-radius:14px;align-items:center;gap:6px;padding:0 10px 0 6px;font-size:12px;line-height:18px;display:inline-flex}.VYJBRq_toggle:hover{background:var(--dsw-alias-interactive-bg-hover)}.VYJBRq_toggleOn{background:var(--dsw-alias-state-business-tertiary);color:var(--dsw-alias-state-business-primary);border-color:#0000}.VYJBRq_check{justify-content:center;align-items:center;width:16px;height:16px;display:inline-flex}.VYJBRq_knob{align-items:center;gap:10px;display:flex}.VYJBRq_knobLabel{width:92px;color:var(--dsw-alias-label-secondary);flex:none;font-size:12px;line-height:18px}.VYJBRq_slider{min-width:0;accent-color:var(--dsw-alias-state-business-primary);flex:1}.VYJBRq_numberWrap{flex:none;align-items:center;gap:4px;display:inline-flex}.VYJBRq_number{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);width:56px;height:26px;color:var(--dsw-alias-label-primary);text-align:right;border-radius:8px;padding:0 6px;font-size:12px;line-height:18px}.VYJBRq_number::-webkit-outer-spin-button,.VYJBRq_number::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}.VYJBRq_unit{width:18px;color:var(--dsw-alias-label-tertiary);flex:none;font-size:12px;line-height:18px}.VYJBRq_segmented{border:1px solid var(--dsw-alias-border-l2);border-radius:8px;display:inline-flex;overflow:hidden}.VYJBRq_seg,.VYJBRq_segActive{height:26px;color:var(--dsw-alias-label-secondary);cursor:pointer;background:0 0;border:none;padding:0 12px;font-size:12px;line-height:18px}.VYJBRq_seg+.VYJBRq_seg,.VYJBRq_segActive+.VYJBRq_seg,.VYJBRq_seg+.VYJBRq_segActive{border-left:1px solid var(--dsw-alias-border-l2)}.VYJBRq_segActive{background:var(--dsw-alias-state-business-tertiary);color:var(--dsw-alias-state-business-primary)}.VYJBRq_wallpaperPick{align-items:center;gap:10px;display:flex}.VYJBRq_fileInput{display:none}.VYJBRq_pickButton{border:1px solid var(--dsw-alias-border-l2);height:26px;color:var(--dsw-alias-label-primary);cursor:pointer;background:0 0;border-radius:8px;padding:0 12px;font-size:12px;line-height:18px}.VYJBRq_pickButton:hover:not(:disabled){background:var(--dsw-alias-interactive-bg-hover)}.VYJBRq_pickButton:disabled{opacity:.55;cursor:progress}.VYJBRq_deleteButton{border:1px solid var(--dsw-alias-border-l2);height:26px;color:var(--dsw-alias-label-error);cursor:pointer;background:0 0;border-radius:8px;padding:0 12px;font-size:12px;line-height:18px}.VYJBRq_deleteButton:hover{background:var(--dsw-alias-interactive-bg-hover)}";
		const tagId$2 = "dsh-client-ui-aqua/AquaAppearanceRow.module.css";
		if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId$2) + "]") === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = "dsh-client-ui-aqua";
			tag.dataset.pluginCss = tagId$2;
			tag.textContent = css$2;
			document.head.appendChild(tag);
		}
		var AquaAppearanceRow_module_css_default = {
			"fileInput": "VYJBRq_fileInput",
			"mediaStatus": "VYJBRq_mediaStatus",
			"subGroup": "VYJBRq_subGroup",
			"toggle": "VYJBRq_toggle",
			"groupHint": "VYJBRq_groupHint",
			"pickButton": "VYJBRq_pickButton",
			"rowHint": "VYJBRq_rowHint",
			"toggleOn": "VYJBRq_toggleOn",
			"group": "VYJBRq_group",
			"number": "VYJBRq_number",
			"row": "VYJBRq_row",
			"rowLabel": "VYJBRq_rowLabel",
			"knobHint": "VYJBRq_knobHint",
			"knob": "VYJBRq_knob",
			"check": "VYJBRq_check",
			"knobLabel": "VYJBRq_knobLabel",
			"slider": "VYJBRq_slider",
			"wallpaperPick": "VYJBRq_wallpaperPick",
			"inlineLabel": "VYJBRq_inlineLabel",
			"numberWrap": "VYJBRq_numberWrap",
			"controls": "VYJBRq_controls",
			"segmented": "VYJBRq_segmented",
			"deleteButton": "VYJBRq_deleteButton",
			"subTitle": "VYJBRq_subTitle",
			"unit": "VYJBRq_unit",
			"seg": "VYJBRq_seg",
			"segActive": "VYJBRq_segActive"
		};
		//#endregion
		//#region src/client/AquaControls.tsx
		/**
		* Shared controls for the Aqua General-settings appearance row: the Knob
		* (stepless slider + number box), a two-option Segmented picker, and the
		* wallpaper file reader. Kept in one file so the row stays a single surface.
		*/
		/** Render one knob row. */
		function Knob({ label, value, min, max, step, unit, onChange }) {
			const clamp = (n) => Math.min(max, Math.max(min, Number.isFinite(n) ? n : min));
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
				className: AquaAppearanceRow_module_css_default.knob,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: AquaAppearanceRow_module_css_default.knobLabel,
						children: label
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
						type: "range",
						className: AquaAppearanceRow_module_css_default.slider,
						min,
						max,
						step,
						value,
						onChange: (e) => {
							onChange(clamp(Number(e.target.value)));
						}
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
						className: AquaAppearanceRow_module_css_default.numberWrap,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
							type: "number",
							className: AquaAppearanceRow_module_css_default.number,
							min,
							max,
							step,
							value,
							onChange: (e) => {
								onChange(clamp(Number(e.target.value)));
							}
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: AquaAppearanceRow_module_css_default.unit,
							children: unit
						})]
					})
				]
			});
		}
		/** Render a two-button segmented picker. */
		function Segmented({ label, value, options, onSelect }) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: AquaAppearanceRow_module_css_default.segmented,
				role: "group",
				"aria-label": label,
				children: options.map((option) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: option.id === value ? AquaAppearanceRow_module_css_default.segActive : AquaAppearanceRow_module_css_default.seg,
					"aria-pressed": option.id === value,
					onClick: () => {
						onSelect(option.id);
					},
					children: option.label
				}, option.id))
			});
		}
		const WALLPAPER_IMAGE_ACCEPT = ".jpg,.jpeg,.png,.webp,.gif,.avif,image/jpeg,image/png,image/webp,image/gif,image/avif";
		const WALLPAPER_VIDEO_ACCEPT = ".mp4,.webm,.ogg,.mov,.m4v,.mkv,video/mp4,video/webm,video/ogg,video/quicktime,video/x-m4v,video/x-matroska";
		const WALLPAPER_IMAGE_EXTENSIONS = /* @__PURE__ */ new Set(["jpg", "jpeg", "png", "webp", "gif", "avif"]);
		const WALLPAPER_VIDEO_EXTENSIONS = /* @__PURE__ */ new Set(["mp4", "webm", "ogg", "mov", "m4v", "mkv"]);
		const MAX_WALLPAPER_IMAGE_BYTES = 30 * 1024 * 1024;
		const MAX_WALLPAPER_VIDEO_BYTES = 200 * 1024 * 1024;
		const VIDEO_PROBE_TIMEOUT_MS = 12e3;
		function wallpaperFileExtension(file) {
			return file.name.toLowerCase().split(".").pop() ?? "";
		}
		function validateWallpaperFile(file, kind) {
			const extension = wallpaperFileExtension(file);
			const allowed = kind === "image" ? WALLPAPER_IMAGE_EXTENSIONS : WALLPAPER_VIDEO_EXTENSIONS;
			const maxBytes = kind === "image" ? MAX_WALLPAPER_IMAGE_BYTES : MAX_WALLPAPER_VIDEO_BYTES;
			if (!allowed.has(extension)) throw /* @__PURE__ */ new Error(`${kind.toUpperCase()}_UNSUPPORTED`);
			if (file.size > maxBytes) throw /* @__PURE__ */ new Error(`${kind.toUpperCase()}_TOO_LARGE`);
		}
		function wallpaperMediaErrorText(t, kind, error) {
			const code = error instanceof Error ? error.message : "";
			if (code === "IMAGE_UNSUPPORTED") return t("aqua.imageUnsupported");
			if (code === "VIDEO_UNSUPPORTED") return t("aqua.videoUnsupported");
			if (code === "IMAGE_TOO_LARGE") return t("aqua.imageTooLarge");
			if (code === "VIDEO_TOO_LARGE") return t("aqua.videoTooLarge");
			if (code === "VIDEO_STORAGE_FAILED") return t("aqua.videoStorageFailed");
			if (code === "WALLPAPER_STORAGE_FAILED") return t(kind === "image" ? "aqua.imageStorageFailed" : "aqua.videoStorageFailed");
			return t(kind === "image" ? "aqua.imageDecodeFailed" : "aqua.videoDecodeFailed");
		}
		/** Read a file, downscale to ≤1920px, and return a compact JPEG data URL. */
		async function fileToDataUrl(file) {
			const raw = await new Promise((resolve, reject) => {
				const reader = new FileReader();
				reader.onload = () => {
					resolve(String(reader.result));
				};
				reader.onerror = () => {
					reject(reader.error);
				};
				reader.readAsDataURL(file);
			});
			const image = await new Promise((resolve, reject) => {
				const im = new Image();
				im.onload = () => {
					resolve(im);
				};
				im.onerror = () => {
					reject(/* @__PURE__ */ new Error("IMAGE_DECODE_FAILED"));
				};
				im.src = raw;
			});
			const scale = Math.min(1, 1920 / Math.max(image.width, image.height));
			const w = Math.max(1, Math.round(image.width * scale));
			const h = Math.max(1, Math.round(image.height * scale));
			const canvas = document.createElement("canvas");
			canvas.width = w;
			canvas.height = h;
			const ctx = canvas.getContext("2d");
			if (ctx === null) throw /* @__PURE__ */ new Error("IMAGE_DECODE_FAILED");
			ctx.drawImage(image, 0, 0, w, h);
			return canvas.toDataURL("image/jpeg", .82);
		}
		/** Confirm that the current browser can decode a selected background video. */
		async function probeWallpaperVideo(file) {
			await new Promise((resolve, reject) => {
				const video = document.createElement("video");
				const url = URL.createObjectURL(file);
				let settled = false;
				const finish = (error) => {
					if (settled) return;
					settled = true;
					window.clearTimeout(timer);
					video.removeAttribute("src");
					video.load();
					URL.revokeObjectURL(url);
					if (error === void 0) resolve();
					else reject(error);
				};
				const timer = window.setTimeout(() => {
					finish(/* @__PURE__ */ new Error("VIDEO_DECODE_FAILED"));
				}, VIDEO_PROBE_TIMEOUT_MS);
				video.preload = "metadata";
				video.muted = true;
				video.playsInline = true;
				video.onloadedmetadata = () => {
					if (video.videoWidth > 0 && video.videoHeight > 0) finish();
					else finish(/* @__PURE__ */ new Error("VIDEO_DECODE_FAILED"));
				};
				video.onerror = () => {
					finish(/* @__PURE__ */ new Error("VIDEO_DECODE_FAILED"));
				};
				video.src = url;
				video.load();
			});
		}
		//#endregion
		//#region src/client/wallpaper-store.ts
		/**
		* Large wallpaper storage: videos too big for localStorage (its ~5MB quota)
		* go into IndexedDB as raw blobs, while the setting keeps a tiny `idb:<id>`
		* marker. On boot the layer loads the blob, wraps it in an object URL and
		* hands it to the <video> element — no quota trouble, survives restarts.
		*/
		const DB_NAME = "dsh-aqua-media";
		const STORE = "wallpaper";
		const DB_VERSION = 1;
		/** Fixed key holding the File System Access handle (the browser's remembered
		*  file authorization — the closest the web allows to "remember the path"). */
		const HANDLE_KEY = "videoHandle";
		function openDb() {
			return new Promise((resolve, reject) => {
				const request = indexedDB.open(DB_NAME, DB_VERSION);
				request.onupgradeneeded = () => {
					const db = request.result;
					if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
				};
				request.onsuccess = () => {
					resolve(request.result);
				};
				request.onerror = () => {
					reject(request.error ?? /* @__PURE__ */ new Error("indexedDB open failed"));
				};
			});
		}
		function tx(db, mode) {
			return db.transaction(STORE, mode).objectStore(STORE);
		}
		/** Store a blob and return its `idb:<id>` marker ('' on failure → caller
		*  falls back to the data-URL path). */
		async function saveVideoBlob(blob) {
			let db;
			try {
				db = await openDb();
				const id = `v${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
				await new Promise((resolve, reject) => {
					const transaction = db.transaction(STORE, "readwrite");
					transaction.oncomplete = () => resolve();
					transaction.onabort = transaction.onerror = () => reject(transaction.error ?? new Error("blob put failed"));
					transaction.objectStore(STORE).put(blob, id);
				});
				return `idb:${id}`;
			} catch {
				return "";
			} finally {
				db?.close();
			}
		}
		/** Load a stored blob by id (null when absent). */
		async function loadVideoBlob(id) {
			let db;
			try {
				db = await openDb();
				const blob = await new Promise((resolve, reject) => {
					const request = tx(db, "readonly").get(id);
					request.onsuccess = () => {
						resolve(request.result);
					};
					request.onerror = () => {
						reject(request.error ?? /* @__PURE__ */ new Error("blob get failed"));
					};
				});
				return blob ?? null;
			} catch {
				return null;
			} finally {
				db?.close();
			}
		}
		/** Drop a stored blob (ignores failures). */
		async function deleteVideoBlob(id) {
			let db;
			try {
				db = await openDb();
				await new Promise((resolve) => {
					const transaction = db.transaction(STORE, "readwrite");
					transaction.oncomplete = transaction.onabort = transaction.onerror = () => resolve();
					transaction.objectStore(STORE).delete(id);
				});
			} catch {} finally {
				db?.close();
			}
		}
		/** Persist a File System Access handle so the next visit can re-read the
		*  ORIGINAL file without the user picking it again. */
		async function saveVideoHandle(handle) {
			try {
				const db = await openDb();
				await new Promise((resolve, reject) => {
					const request = tx(db, "readwrite").put(handle, HANDLE_KEY);
					request.onsuccess = () => {
						resolve();
					};
					request.onerror = () => {
						reject(request.error ?? /* @__PURE__ */ new Error("handle put failed"));
					};
				});
				db.close();
				return true;
			} catch {
				return false;
			}
		}
		/** Load the remembered file handle (null when absent or storage fails). */
		async function loadVideoHandle() {
			let db;
			try {
				db = await openDb();
				const handle = await new Promise((resolve, reject) => {
					const request = tx(db, "readonly").get(HANDLE_KEY);
					request.onsuccess = () => {
						resolve(request.result);
					};
					request.onerror = () => {
						reject(request.error ?? /* @__PURE__ */ new Error("handle get failed"));
					};
				});
				return handle ?? null;
			} catch {
				return null;
			} finally {
				db?.close();
			}
		}
		//#endregion
		//#region src/client/AquaAppearanceRow.tsx
		/**
		* Aqua controls rendered inside the standalone Plugins settings tab: every glass knob — mode
		* (mica / compatibility), blur/frost (mica mode only), fluid color,
		* background brightness, the backdrop source picker, and the wallpaper
		* picker with its two knobs. Every
		* write goes straight through to the layer, so the skin moves live. The
		* and the whole controls block renders nothing while the master switch is off.
		*/
		/**
		* Render the Aqua appearance row.
		* @param props - composed slot props.
		* @returns the plugin controls tree.
		*/
		function AquaAppearanceRow(props) {
			const { t, setEnabled, setMode, setBlur, setFrost, setOntologySurfaceStrength, setFluidHue, setFluidDepth, setBgBrightness, setBackground, setWallpaper, setMesh, setSpotlight, setPress, setWallpaperBlur, setWallpaperFrost, setVideoBlur, setVideoBrightness, useStore } = props;
			const enabled = useStore((s) => s.enabled);
			const mode = useStore((s) => s.mode);
			const blur = useStore((s) => s.blur);
			const frost = useStore((s) => s.frost);
			const ontologySurfaceStrength = useStore((s) => s.ontologySurfaceStrength);
			const fluidHue = useStore((s) => s.fluidHue);
			const fluidDepth = useStore((s) => s.fluidDepth);
			const bgBrightness = useStore((s) => s.bgBrightness);
			const dark = useStore((s) => s.dark);
			const background = useStore((s) => s.background);
			const mesh = useStore((s) => s.mesh);
			const spotlight = useStore((s) => s.spotlight);
			const press = useStore((s) => s.press);
			const wallpaper = useStore((s) => s.wallpaper);
			const mediaError = useStore((s) => s.mediaError);
			const wallpaperBlur = useStore((s) => s.wallpaperBlur);
			const wallpaperFrost = useStore((s) => s.wallpaperFrost);
			const videoBlur = useStore((s) => s.videoBlur);
			const videoBrightness = useStore((s) => s.videoBrightness);
			const fileRef = (0, react.useRef)(null);
			const videoRef = (0, react.useRef)(null);
			const mediaRequest = (0, react.useRef)(0);
			const mediaInputState = (0, react.useRef)({ enabled, background });
			mediaInputState.current = { enabled, background };
			(0, react.useEffect)(() => () => { mediaRequest.current += 1; }, []);
			const mediaRequestIsCurrent = (request) => request === mediaRequest.current && mediaInputState.current.enabled && mediaInputState.current.background === "wallpaper";
			const [mediaFeedback, setMediaFeedback] = (0, react.useState)({
				tone: "idle",
				message: ""
			});
			(0, react.useEffect)(() => {
				if (mediaError) setMediaFeedback({ tone: "error", message: t(mediaError) });
			}, [mediaError]);
			const mediaBusy = mediaFeedback.tone === "loading";
			const isVideoWallpaper = wallpaper.startsWith("data:video/") || wallpaper.startsWith("idb:") || wallpaper.startsWith("fsa:");
			const applyWallpaperImage = async (file) => {
				const request = ++mediaRequest.current;
				setMediaFeedback({
					tone: "loading",
					message: t("aqua.mediaProcessing", { name: file.name })
				});
				try {
					validateWallpaperFile(file, "image");
					const dataUrl = await fileToDataUrl(file);
					if (!mediaRequestIsCurrent(request)) {
						if (request === mediaRequest.current) setMediaFeedback({ tone: "idle", message: "" });
						return;
					}
					setWallpaper(dataUrl);
					setBackground("wallpaper");
					setMediaFeedback({
						tone: "success",
						message: t("aqua.imageApplied", { name: file.name })
					});
				} catch (error) {
					if (request !== mediaRequest.current) return;
					setMediaFeedback({
						tone: "error",
						message: wallpaperMediaErrorText(t, "image", error)
					});
				}
			};
			const applyWallpaperVideo = async (file) => {
				const request = ++mediaRequest.current;
				let storedMarker;
				setMediaFeedback({
					tone: "loading",
					message: t("aqua.mediaProcessing", { name: file.name })
				});
				try {
					validateWallpaperFile(file, "video");
					await probeWallpaperVideo(file);
					if (!mediaRequestIsCurrent(request)) {
						if (request === mediaRequest.current) setMediaFeedback({ tone: "idle", message: "" });
						return;
					}
					const id = await saveVideoBlob(file);
					if (id === "") throw /* @__PURE__ */ new Error("VIDEO_STORAGE_FAILED");
					if (!mediaRequestIsCurrent(request)) {
						await deleteVideoBlob(id.slice(4));
						if (request === mediaRequest.current) setMediaFeedback({ tone: "idle", message: "" });
						return;
					}
					storedMarker = id;
					setWallpaper(id);
					setBackground("wallpaper");
					storedMarker = void 0;
					setMediaFeedback({
						tone: "success",
						message: t("aqua.videoApplied", { name: file.name })
					});
				} catch (error) {
					if (storedMarker) await deleteVideoBlob(storedMarker.slice(4));
					if (request !== mediaRequest.current) return;
					setMediaFeedback({
						tone: "error",
						message: wallpaperMediaErrorText(t, "video", error)
					});
				}
			};
			if (!enabled) return null;
			const bgMin = dark ? 0 : 50;
			const bgMax = dark ? 50 : 100;
			const bgDisplay = Math.min(bgMax, Math.max(bgMin, bgBrightness));
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: AquaAppearanceRow_module_css_default.group,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: AquaAppearanceRow_module_css_default.subGroup,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: AquaAppearanceRow_module_css_default.subTitle,
							children: t("aqua.mode")
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: AquaAppearanceRow_module_css_default.controls,
							children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
								className: AquaAppearanceRow_module_css_default.row,
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Segmented, {
									label: t("aqua.mode"),
									value: mode,
									options: [{
										id: "mica",
										label: t("aqua.modeMica")
									}, {
										id: "compat",
										label: t("aqua.modeCompat")
									}],
									onSelect: setMode
								})
							})
						})]
					}),
					mode === "mica" && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: AquaAppearanceRow_module_css_default.subGroup,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: AquaAppearanceRow_module_css_default.subTitle,
							children: t("aqua.materialGroup")
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: AquaAppearanceRow_module_css_default.controls,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
								label: t("aqua.blur"),
								value: blur,
								min: 0,
								max: 40,
								step: .5,
								unit: "px",
								onChange: setBlur
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
								label: t("aqua.frost"),
								value: frost,
								min: 0,
								max: 100,
								step: 1,
								unit: "%",
								onChange: setFrost
							})]
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: AquaAppearanceRow_module_css_default.subGroup,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: AquaAppearanceRow_module_css_default.subTitle,
							children: t("aqua.ontologyGroup")
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: AquaAppearanceRow_module_css_default.controls,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
								label: t("aqua.ontologySurfaceStrength"),
								value: ontologySurfaceStrength,
								min: 0,
								max: 100,
								step: 1,
								unit: "%",
								onChange: setOntologySurfaceStrength
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
								className: AquaAppearanceRow_module_css_default.knobHint,
								children: t("aqua.ontologySurfaceStrengthHint")
							})]
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: AquaAppearanceRow_module_css_default.subGroup,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: AquaAppearanceRow_module_css_default.subTitle,
							children: t("aqua.background")
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: AquaAppearanceRow_module_css_default.controls,
							children: [
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
									className: AquaAppearanceRow_module_css_default.row,
									children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Segmented, {
										label: t("aqua.background"),
										value: background,
										options: [{
											id: "fluid",
											label: t("aqua.backgroundFluid")
										}, {
											id: "wallpaper",
											label: t("aqua.backgroundWallpaper")
										}],
										onSelect: (value) => {
											try { setBackground(value); }
											catch (error) { setMediaFeedback({ tone: "error", message: wallpaperMediaErrorText(t, isVideoWallpaper ? "video" : "image", error) }); }
										}
									})
								}),
								background === "fluid" && mediaFeedback.tone === "error" && (0, react_jsx_runtime.jsx)("div", {
									className: AquaAppearanceRow_module_css_default.mediaStatus,
									role: "alert", "aria-live": "polite", "data-tone": "error", children: mediaFeedback.message
								}),
								background === "fluid" && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
									label: t("aqua.fluidHue"),
									value: fluidHue,
									min: 0,
									max: 360,
									step: 1,
									unit: "°",
									onChange: setFluidHue
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
									label: t("aqua.fluidDepth"),
									value: fluidDepth,
									min: 0,
									max: 100,
									step: 1,
									unit: "%",
									onChange: setFluidDepth
								})] }),
								background === "wallpaper" && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [
									/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
										className: AquaAppearanceRow_module_css_default.row,
										children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
											className: AquaAppearanceRow_module_css_default.rowLabel,
											children: t("aqua.wallpaper")
										}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
											className: AquaAppearanceRow_module_css_default.wallpaperPick,
											children: [
												/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
													ref: fileRef,
													type: "file",
													accept: WALLPAPER_IMAGE_ACCEPT,
													className: AquaAppearanceRow_module_css_default.fileInput,
													onChange: (e) => {
														const file = e.target.files?.[0];
														if (file !== void 0) void applyWallpaperImage(file);
														e.target.value = "";
													}
												}),
												/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
													ref: videoRef,
													type: "file",
													accept: WALLPAPER_VIDEO_ACCEPT,
													className: AquaAppearanceRow_module_css_default.fileInput,
													onChange: (e) => {
														const file = e.target.files?.[0];
														if (file !== void 0) void applyWallpaperVideo(file);
														e.target.value = "";
													}
												}),
												/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
													type: "button",
													className: AquaAppearanceRow_module_css_default.pickButton,
													disabled: mediaBusy,
													onClick: () => {
														fileRef.current?.click();
													},
													children: t("aqua.chooseImage")
												}),
												/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
													type: "button",
													className: AquaAppearanceRow_module_css_default.pickButton,
													disabled: mediaBusy,
													onClick: () => {
														videoRef.current?.click();
													},
													children: t("aqua.chooseVideo")
												}),
												wallpaper !== "" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
													type: "button",
													className: AquaAppearanceRow_module_css_default.deleteButton,
													onClick: () => {
														try {
															mediaRequest.current += 1;
															setWallpaper("");
															setMediaFeedback({ tone: "idle", message: "" });
														} catch (error) {
															setMediaFeedback({ tone: "error", message: wallpaperMediaErrorText(t, isVideoWallpaper ? "video" : "image", error) });
														}
													},
													children: t("aqua.deleteWallpaper")
												})
											]
										})]
									}),
									/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
									className: AquaAppearanceRow_module_css_default.knobHint,
									children: t("aqua.wallpaperHint")
								}),
								mediaFeedback.message !== "" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
									className: AquaAppearanceRow_module_css_default.mediaStatus,
									role: mediaFeedback.tone === "error" ? "alert" : "status",
									"aria-live": "polite",
									"data-tone": mediaFeedback.tone,
									children: mediaFeedback.message
								}),
									!isVideoWallpaper && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
										label: t("aqua.wallpaperBlur"),
										value: wallpaperBlur,
										min: 0,
										max: 40,
										step: .5,
										unit: "px",
										onChange: setWallpaperBlur
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
										label: t("aqua.wallpaperFrost"),
										value: wallpaperFrost,
										min: 0,
										max: 100,
										step: 1,
										unit: "%",
										onChange: setWallpaperFrost
									})] }),
									isVideoWallpaper && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [
										/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
											label: t("aqua.videoBlur"),
											value: videoBlur,
											min: 0,
											max: 40,
											step: .5,
											unit: "px",
											onChange: setVideoBlur
										}),
										/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
											label: t("aqua.videoBrightness"),
											value: videoBrightness,
											min: 0,
											max: 100,
											step: 1,
											unit: "%",
											onChange: setVideoBrightness
										}),
										/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
											className: AquaAppearanceRow_module_css_default.knobHint,
											children: t("aqua.videoHint")
										})
									] })
								] }),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Knob, {
									label: t("aqua.bgBrightness"),
									value: bgDisplay,
									min: bgMin,
									max: bgMax,
									step: 1,
									unit: "%",
									onChange: setBgBrightness
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
									className: AquaAppearanceRow_module_css_default.knobHint,
									children: t(dark ? "aqua.bgBrightnessHintDark" : "aqua.bgBrightnessHintLight")
								})
							]
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: AquaAppearanceRow_module_css_default.subGroup,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: AquaAppearanceRow_module_css_default.subTitle,
							children: t("aqua.decorAmbient")
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: AquaAppearanceRow_module_css_default.controls,
							children: [
								/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: AquaAppearanceRow_module_css_default.row,
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: AquaAppearanceRow_module_css_default.rowLabel,
										children: t("aqua.mesh")
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
										type: "button",
										className: mesh ? AquaAppearanceRow_module_css_default.toggleOn : AquaAppearanceRow_module_css_default.toggle,
										"aria-pressed": mesh,
										onClick: () => {
											setMesh(!mesh);
										},
										children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
											className: AquaAppearanceRow_module_css_default.check,
										children: mesh && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(AquaCheckIcon, { size: 16 })
										}), mesh ? t("aqua.enable") : t("aqua.disable")]
									})]
								})
							]
						})]
					}),
					mode === "mica" && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: AquaAppearanceRow_module_css_default.subGroup,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: AquaAppearanceRow_module_css_default.subTitle,
							children: t("aqua.decorHover")
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: AquaAppearanceRow_module_css_default.controls,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: AquaAppearanceRow_module_css_default.row,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: AquaAppearanceRow_module_css_default.rowLabel,
									children: t("aqua.spotlight")
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
									type: "button",
									className: spotlight ? AquaAppearanceRow_module_css_default.toggleOn : AquaAppearanceRow_module_css_default.toggle,
									"aria-pressed": spotlight,
									onClick: () => {
										setSpotlight(!spotlight);
									},
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: AquaAppearanceRow_module_css_default.check,
										children: spotlight && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(AquaCheckIcon, { size: 16 })
									}), spotlight ? t("aqua.enable") : t("aqua.disable")]
								})]
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: AquaAppearanceRow_module_css_default.row,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: AquaAppearanceRow_module_css_default.rowLabel,
									children: t("aqua.press")
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
									type: "button",
									className: press ? AquaAppearanceRow_module_css_default.toggleOn : AquaAppearanceRow_module_css_default.toggle,
									"aria-pressed": press,
									onClick: () => {
										setPress(!press);
									},
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: AquaAppearanceRow_module_css_default.check,
										children: press && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(AquaCheckIcon, { size: 16 })
									}), press ? t("aqua.enable") : t("aqua.disable")]
								})]
							})]
						})]
					})
				]
			});
		}
		//#endregion
		//#region src/client/settings-store.ts
		/**
		* Aqua row slot store: a mirror of the layer's state (enable flag plus the
		* knobs and the backdrop source). The plugin's apply-world change listener is
		* the only writer; the row component reads via props.useStore.
		*/
		/**
		* Declares the Aqua row state and write surface.
		* @returns the store handle.
		*/
		function createAquaRowStore() {
			return (0, _deepseek_ai_dsh_client_store.defineStore)({
				init: () => ({
					enabled: true,
					mode: "mica",
					blur: 20,
					frost: 7,
					ontologySurfaceStrength: 60,
					fluidHue: 320,
					fluidDepth: 25,
					bgBrightness: 50,
					dark: false,
					background: "fluid",
					wallpaper: "",
					mesh: false,
					spotlight: true,
					press: true,
					wallpaperBlur: 0,
					wallpaperFrost: 0,
					videoBlur: 6,
					videoBrightness: 45,
					mediaError: "",
					revision: -1
				}),
				actions: { sync: (d, next, revision) => {
					if (revision <= d.revision) return;
					d.enabled = next.enabled;
					d.mode = next.mode;
					d.blur = next.blur;
					d.frost = next.frost;
					d.ontologySurfaceStrength = next.ontologySurfaceStrength;
					d.fluidHue = next.fluidHue;
					d.fluidDepth = next.fluidDepth;
					d.bgBrightness = next.bgBrightness;
					d.dark = next.dark;
					d.background = next.background;
					d.wallpaper = next.wallpaper;
					d.mesh = next.mesh;
					d.spotlight = next.spotlight;
					d.press = next.press;
					d.wallpaperBlur = next.wallpaperBlur;
					d.wallpaperFrost = next.wallpaperFrost;
					d.videoBlur = next.videoBlur;
					d.videoBrightness = next.videoBrightness;
					d.mediaError = next.mediaError;
					d.revision = revision;
				} }
			});
		}
		//#endregion
		//#region src/client/locales.ts
		/** `settings.aqua` namespace dictionaries (the settings-row copy). */
		/** Dictionary namespace owned by this plugin. */
		const NS = "settings.aqua";
		/** Simplified Chinese dictionary (the key-set source of truth). */
		const zh = {
			"aqua.tab": "界面插件",
			"aqua.title": "玻璃主题",
			"aqua.description": "全局玻璃质感，云母/兼容双模式，模糊度、磨砂度、背景与颜色都可自由调节",
			"aqua.enable": "开启",
			"aqua.disable": "关闭",
			"aqua.mode": "模式",
			"aqua.modeMica": "云母效果",
			"aqua.modeCompat": "兼容模式",
			"aqua.materialGroup": "玻璃材质",
			"aqua.decorAmbient": "环境装饰",
			"aqua.decorHover": "悬停效果",
			"aqua.mesh": "网状交互",
			"aqua.spotlight": "鼠标辉光",
			"aqua.press": "悬停下压",
			"aqua.blur": "玻璃模糊度",
			"aqua.frost": "磨砂度",
			"aqua.ontologyGroup": "本体中心",
			"aqua.ontologySurfaceStrength": "底板强度",
			"aqua.ontologySurfaceStrengthHint": "调整工程中心、S0–S7 和本体管理的底板透明度；不改变壁纸和其他模块",
			"aqua.fluidHue": "色调",
			"aqua.fluidDepth": "颜色深浅",
			"aqua.bgBrightness": "背景亮度",
			"aqua.bgBrightnessHintDark": "深色模式：0 压暗至纯黑，50 原样",
			"aqua.bgBrightnessHintLight": "浅色模式：50 原样，100 提亮至纯白",
			"aqua.background": "背景",
			"aqua.backgroundFluid": "流体",
			"aqua.backgroundWallpaper": "壁纸",
			"aqua.wallpaper": "壁纸",
			"aqua.wallpaperHint": "浅色壁纸用浅色模式，深色壁纸用深色模式⚠️",
			"aqua.chooseImage": "选择图片",
			"aqua.chooseVideo": "选择视频",
			"aqua.deleteWallpaper": "删除",
			"aqua.mediaProcessing": "正在处理 {name}…",
			"aqua.imageApplied": "图片已应用：{name}",
			"aqua.videoApplied": "视频已应用：{name}",
			"aqua.imageUnsupported": "不支持该图片格式，请选择 JPG、PNG、WebP、GIF 或 AVIF。",
			"aqua.videoUnsupported": "不支持该视频格式，请选择 MP4、WebM、OGG、MOV、M4V 或 MKV。",
			"aqua.imageTooLarge": "图片超过 30 MB，请压缩后重试。",
			"aqua.videoTooLarge": "视频超过 200 MB，请裁剪或压缩后重试。",
			"aqua.imageDecodeFailed": "图片无法读取，请换成浏览器支持的 JPG、PNG、WebP、GIF 或 AVIF。",
			"aqua.videoDecodeFailed": "当前浏览器无法播放该视频编码，请转成 H.264 MP4 或 WebM 后重试。",
			"aqua.videoStorageFailed": "视频无法保存到浏览器本地存储，请释放空间或换一个较小的视频。",
			"aqua.imageStorageFailed": "图片无法保存到浏览器本地存储，已保留原背景。请释放空间或选择较小的图片。",
			"aqua.mediaUnavailable": "保存的壁纸无法读取，已恢复流体背景。请重新选择图片或视频。",
			"aqua.videoPermissionRequired": "保存的视频读取权限不可用，已恢复流体背景。请重新选择视频。",
			"aqua.videoPlaybackFailed": "视频背景播放失败，已恢复流体背景。请重新选择可播放的视频。",
			"aqua.wallpaperBlur": "壁纸模糊度",
			"aqua.wallpaperFrost": "壁纸磨砂度",
			"aqua.videoBlur": "视频模糊度",
			"aqua.videoBrightness": "视频亮度",
			"aqua.videoHint": "视频会静音循环播放并自动压暗；可调节模糊度和亮度。减少动态时显示静态首帧，窗口隐藏时暂停。"
		};
		/** English dictionary. */
		const en = {
			"aqua.tab": "Appearance",
			"aqua.title": "Glass theme",
			"aqua.description": "Global glassmorphism with mica/compatibility modes — blur, frost, backdrop, and color all adjustable",
			"aqua.enable": "On",
			"aqua.disable": "Off",
			"aqua.mode": "Mode",
			"aqua.modeMica": "Mica",
			"aqua.modeCompat": "Compatibility",
			"aqua.materialGroup": "Glass material",
			"aqua.decorAmbient": "Ambient",
			"aqua.decorHover": "Hover effects",
			"aqua.mesh": "Interactive mesh",
			"aqua.spotlight": "Cursor glow",
			"aqua.press": "Hover tilt",
			"aqua.blur": "Glass blur",
			"aqua.frost": "Frost",
			"aqua.ontologyGroup": "Ontology Center",
			"aqua.ontologySurfaceStrength": "Surface strength",
			"aqua.ontologySurfaceStrengthHint": "Adjusts Engineering, S0–S7, and Registry surfaces without changing the wallpaper or other modules",
			"aqua.fluidHue": "Hue",
			"aqua.fluidDepth": "Color depth",
			"aqua.bgBrightness": "Background brightness",
			"aqua.bgBrightnessHintDark": "Dark mode: 0 fades to pure black, 50 is unchanged",
			"aqua.bgBrightnessHintLight": "Light mode: 50 is unchanged, 100 brightens to pure white",
			"aqua.background": "Backdrop",
			"aqua.backgroundFluid": "Fluid",
			"aqua.backgroundWallpaper": "Wallpaper",
			"aqua.wallpaper": "Wallpaper",
			"aqua.wallpaperHint": "Use light mode for light wallpapers, dark mode for dark wallpapers ⚠️",
			"aqua.chooseImage": "Choose image",
			"aqua.chooseVideo": "Choose video",
			"aqua.deleteWallpaper": "Delete",
			"aqua.mediaProcessing": "Processing {name}…",
			"aqua.imageApplied": "Image applied: {name}",
			"aqua.videoApplied": "Video applied: {name}",
			"aqua.imageUnsupported": "Unsupported image. Choose JPG, PNG, WebP, GIF, or AVIF.",
			"aqua.videoUnsupported": "Unsupported video. Choose MP4, WebM, OGG, MOV, M4V, or MKV.",
			"aqua.imageTooLarge": "The image is over 30 MB. Compress it and try again.",
			"aqua.videoTooLarge": "The video is over 200 MB. Trim or compress it and try again.",
			"aqua.imageDecodeFailed": "This image cannot be decoded. Try JPG, PNG, WebP, GIF, or AVIF.",
			"aqua.videoDecodeFailed": "This browser cannot play that codec. Convert it to H.264 MP4 or WebM.",
			"aqua.videoStorageFailed": "The video could not be stored locally. Free browser storage or choose a smaller video.",
			"aqua.imageStorageFailed": "The image could not be saved locally. The previous background is unchanged; free browser storage or choose a smaller image.",
			"aqua.mediaUnavailable": "The saved wallpaper could not be read. The fluid background has been restored; choose an image or video again.",
			"aqua.videoPermissionRequired": "The saved video cannot be accessed. The fluid background has been restored; select the video again.",
			"aqua.videoPlaybackFailed": "The video background could not play. The fluid background has been restored; select a playable video.",
			"aqua.wallpaperBlur": "Wallpaper blur",
			"aqua.wallpaperFrost": "Wallpaper frost",
			"aqua.videoBlur": "Video blur",
			"aqua.videoBrightness": "Video brightness",
			"aqua.videoHint": "Video wallpapers loop muted and are dimmed automatically. Reduced motion shows a still frame; hidden windows pause playback."
		};
		//#endregion
		//#region src/client/ambient-scene.ts
		// ORION uses only the fluid canvas. No upstream brand-shaped decoration is bundled.
		const AMBIENT_SCENE = "<canvas data-dsh-aqua-fluid-canvas></canvas>";
		/** Build the ambient container element (or reuse an existing one). */
		function ensureAmbientScene() {
			const existing = document.querySelector("[data-dsh-aqua-ambient]");
			if (existing !== null) return existing;
			const holder = document.createElement("div");
			holder.innerHTML = `<div data-dsh-aqua-ambient aria-hidden="true">${AMBIENT_SCENE}</div>`;
			const node = holder.firstElementChild;
			if (!(node instanceof HTMLElement)) throw new Error("ui-aqua: ambient scene markup failed to parse");
			document.body.prepend(node);
			if (document.querySelector("[data-dsh-aqua-wallpaper-layer]") === null) {
				const wallpaper = document.createElement("div");
				wallpaper.setAttribute("data-dsh-aqua-wallpaper", "");
				wallpaper.setAttribute("data-dsh-aqua-wallpaper-layer", "");
				wallpaper.setAttribute("aria-hidden", "true");
				wallpaper.hidden = true;
				wallpaper.innerHTML = "<img data-dsh-aqua-wallpaper-img alt=\"\" hidden><video data-dsh-aqua-wallpaper-video loop playsinline preload=\"auto\" hidden></video>";
				document.body.prepend(wallpaper);
			}
			return node;
		}
		/** Remove the ambient container wherever it lives. */
		function removeAmbientScene() {
			for (const node of document.querySelectorAll("[data-dsh-aqua-ambient]")) node.remove();
			for (const node of document.querySelectorAll("[data-dsh-aqua-wallpaper-layer]")) node.remove();
		}
		/** Add the page edge-fade bands (5px gradient blur over the chat content). */
		function ensurePageFades() {
			if (document.querySelector("[data-dsh-aqua-fade]") !== null) return;
			const top = document.createElement("div");
			top.setAttribute("data-dsh-aqua-fade", "top");
			top.setAttribute("aria-hidden", "true");
			const bottom = document.createElement("div");
			bottom.setAttribute("data-dsh-aqua-fade", "bottom");
			bottom.setAttribute("aria-hidden", "true");
			document.body.appendChild(top);
			document.body.appendChild(bottom);
		}
		/** Remove the edge-fade bands. */
		function removePageFades() {
			for (const el of document.querySelectorAll("[data-dsh-aqua-fade]")) el.remove();
		}
		//#endregion
		//#region src/client/fluid-shader.ts
		/** The exact default parameter set shipped by the site. */
		const SITE_FLUID_PARAMS = {
			mouseRadius: .22,
			mouseStrength: 1.1,
			decay: .96,
			distortBoost: 1.35,
			noiseBoost: 0,
			swirlBoost: .45,
			speed: 14,
			distortion: 20,
			swirl: 12,
			swirlIterations: 8,
			scale: .5,
			rotation: -5,
			proportion: 50,
			softness: 100,
			shapeScale: 10,
			offsetX: 0,
			offsetY: 65,
			color1: "#8AA3D6",
			color2: "#FFFFFF",
			color3: "#FFFFFF"
		};
		const VERTEX_SHADER = `#version 300 es
in vec4 a_position;
out vec2 vUv;
void main() {
  vUv = a_position.xy * 0.5 + 0.5;
  gl_Position = a_position;
}
`;
		const FLOW_SHADER = `#version 300 es
precision mediump float;
in vec2 vUv;
uniform sampler2D u_prev;
uniform vec2 u_mouse;
uniform vec2 u_velocity;
uniform float u_brushRadius;
uniform float u_brushStrength;
uniform float u_decay;
out vec4 fragColor;

void main() {
  vec4 prev = texture(u_prev, vUv);

  prev.r *= u_decay;
  prev.gb = mix(vec2(0.5), prev.gb, u_decay);

  float dist = distance(vUv, u_mouse);

  float influence = exp(-dist * dist / (u_brushRadius * u_brushRadius * 0.5));
  influence = max(0.0, influence - 0.01);

  float speed = length(u_velocity);
  float presenceStrength = u_brushStrength * 0.3;
  float velBonus = min(speed * 3.0, 0.7) * u_brushStrength;
  float totalStrength = presenceStrength + velBonus;

  prev.r = max(prev.r, influence * totalStrength);
  float blendAmt = influence * min(totalStrength, 0.4) * 0.3;
  prev.g = mix(prev.g, clamp(u_velocity.x * 2.0 + 0.5, 0.0, 1.0), blendAmt);
  prev.b = mix(prev.b, clamp(u_velocity.y * 2.0 + 0.5, 0.0, 1.0), blendAmt);

  fragColor = prev;
}
`;
		const DISPLAY_SHADER = `#version 300 es
precision mediump float;
in vec2 vUv;
uniform float u_time;
uniform float u_pixelRatio;
uniform vec2 u_resolution;
uniform float u_scale;
uniform float u_rotation;
uniform vec4 u_color1, u_color2, u_color3;
uniform float u_colorCount;
uniform float u_proportion;
uniform float u_softness;
uniform float u_shape;
uniform float u_shapeScale;
uniform float u_distortion;
uniform float u_swirl;
uniform float u_swirlIterations;
uniform vec2 u_offset;
uniform sampler2D u_flowmap;
uniform float u_distortBoost;
uniform float u_noiseBoost;
uniform float u_swirlBoost;
out vec4 fragColor;

#define TWO_PI 6.28318530718
#define PI 3.14159265358979323846

vec2 rotate(vec2 uv, float th) { return mat2(cos(th), sin(th), -sin(th), cos(th)) * uv; }
float random(vec2 st) { return fract(sin(dot(st.xy, vec2(12.9898, 78.233))) * 43758.5453123); }
float noise(vec2 st) {
  vec2 i = floor(st); vec2 f = fract(st);
  float a = random(i), b = random(i + vec2(1,0)), c = random(i + vec2(0,1)), d = random(i + vec2(1,1));
  vec2 u = f*f*(3.0-2.0*f);
  return mix(mix(a,b,u.x), mix(c,d,u.x), u.y);
}

vec3 blend_multi(float mixer, float softness) {
  float edge = 1.0 - softness;
  vec3 col = u_color1.rgb;
  if (u_colorCount > 1.5) { col = mix(col, u_color2.rgb, smoothstep(0.0 + 0.35*edge, 0.7 - 0.35*edge, mixer)); }
  if (u_colorCount > 2.5) { col = mix(col, u_color3.rgb, smoothstep(0.3 + 0.35*edge, 1.0 - 0.35*edge, mixer)); }
  return col;
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_resolution.xy;
  float t = .5 * u_time;
  float ns = .0005 + .006 * u_scale;
  uv -= .5; uv *= (ns * u_resolution); uv = rotate(uv, u_rotation * .5 * PI);
  uv /= u_pixelRatio; uv += .5; uv += u_offset;

  vec2 fragUV = gl_FragCoord.xy / u_resolution.xy;
  vec4 flow = texture(u_flowmap, fragUV);
  float influence = flow.r;
  vec2 flowDir = (flow.gb - 0.5) * 2.0;

  float n1 = noise(uv + t), n2 = noise(uv*2. - t);
  float angle = n1 * TWO_PI;

  float totalDistortion = u_distortion + influence * u_distortBoost;
  uv.x += 4. * totalDistortion * n2 * cos(angle);
  uv.y += 4. * totalDistortion * n2 * sin(angle);

  uv += flowDir * influence * 0.15;

  if (influence > 0.001) {
    float localNoise = noise(uv * 2.0 + t * 1.5);
    uv += influence * u_noiseBoost * vec2(cos(localNoise * TWO_PI), sin(localNoise * TWO_PI));
  }

  float iters = ceil(clamp(u_swirlIterations, 1., 30.));
  float swirlAmt = clamp(u_swirl, 0., 2.) + influence * u_swirlBoost;
  for (float i = 1.; i <= 30.0; i++) {
    if (i > iters) break;
    uv.x += swirlAmt / i * cos(t + i*1.5*uv.y);
    uv.y += swirlAmt / i * cos(t + i*1.*uv.x);
  }

  float proportion = clamp(u_proportion, 0., 1.);
  vec2 cuv = uv * (.5 + 3.5 * u_shapeScale);
  float shape = .5 + .5 * sin(cuv.x) * cos(cuv.y);
  float mixer = shape + .48 * sign(proportion - .5) * pow(abs(proportion - .5), .5);
  vec3 col = blend_multi(mixer, clamp(u_softness, 0., 1.));
  fragColor = vec4(col, 1.0);
}
`;
		function hexToRgb(value) {
			const hex = value.replace("#", "");
			return [
				parseInt(hex.slice(0, 2), 16) / 255,
				parseInt(hex.slice(2, 4), 16) / 255,
				parseInt(hex.slice(4, 6), 16) / 255
			];
		}
		/**
		* Mount the fluid simulation on a canvas and run it until disposed.
		* @param canvas - full-size canvas element (CSS-sized by the ambient layer).
		* @param params - simulation parameters (site defaults are the natural input).
		* @returns the live handle.
		*/
		function attachFluidShader(canvas, params) {
			const gl = canvas.getContext("webgl2", {
				alpha: true,
				premultipliedAlpha: false,
				powerPreference: "low-power"
			});
			if (gl === null) return {
				setParams: () => {},
				stir: () => {},
				dispose: () => {}
			};
			const compile = (type, source) => {
				const shader = gl.createShader(type);
				if (shader === null) return null;
				gl.shaderSource(shader, source);
				gl.compileShader(shader);
				if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
					console.error("ui-aqua fluid shader:", gl.getShaderInfoLog(shader));
					return null;
				}
				return shader;
			};
			const link = (fragment) => {
				const vertex = compile(gl.VERTEX_SHADER, VERTEX_SHADER);
				const frag = compile(gl.FRAGMENT_SHADER, fragment);
				if (vertex === null || frag === null) return null;
				const program = gl.createProgram();
				if (program === null) return null;
				gl.attachShader(program, vertex);
				gl.attachShader(program, frag);
				gl.linkProgram(program);
				if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
					console.error("ui-aqua fluid link:", gl.getProgramInfoLog(program));
					return null;
				}
				return program;
			};
			const flowProgram = link(FLOW_SHADER);
			const displayProgram = link(DISPLAY_SHADER);
			if (flowProgram === null || displayProgram === null) return {
				setParams: () => {},
				stir: () => {},
				dispose: () => {}
			};
			const flow = {
				prev: gl.getUniformLocation(flowProgram, "u_prev"),
				mouse: gl.getUniformLocation(flowProgram, "u_mouse"),
				velocity: gl.getUniformLocation(flowProgram, "u_velocity"),
				brushRadius: gl.getUniformLocation(flowProgram, "u_brushRadius"),
				brushStrength: gl.getUniformLocation(flowProgram, "u_brushStrength"),
				decay: gl.getUniformLocation(flowProgram, "u_decay")
			};
			const display = {
				time: gl.getUniformLocation(displayProgram, "u_time"),
				pixelRatio: gl.getUniformLocation(displayProgram, "u_pixelRatio"),
				resolution: gl.getUniformLocation(displayProgram, "u_resolution"),
				scale: gl.getUniformLocation(displayProgram, "u_scale"),
				rotation: gl.getUniformLocation(displayProgram, "u_rotation"),
				offset: gl.getUniformLocation(displayProgram, "u_offset"),
				color1: gl.getUniformLocation(displayProgram, "u_color1"),
				color2: gl.getUniformLocation(displayProgram, "u_color2"),
				color3: gl.getUniformLocation(displayProgram, "u_color3"),
				colorCount: gl.getUniformLocation(displayProgram, "u_colorCount"),
				proportion: gl.getUniformLocation(displayProgram, "u_proportion"),
				softness: gl.getUniformLocation(displayProgram, "u_softness"),
				shape: gl.getUniformLocation(displayProgram, "u_shape"),
				shapeScale: gl.getUniformLocation(displayProgram, "u_shapeScale"),
				distortion: gl.getUniformLocation(displayProgram, "u_distortion"),
				swirl: gl.getUniformLocation(displayProgram, "u_swirl"),
				swirlIterations: gl.getUniformLocation(displayProgram, "u_swirlIterations"),
				flowmap: gl.getUniformLocation(displayProgram, "u_flowmap"),
				distortBoost: gl.getUniformLocation(displayProgram, "u_distortBoost"),
				noiseBoost: gl.getUniformLocation(displayProgram, "u_noiseBoost"),
				swirlBoost: gl.getUniformLocation(displayProgram, "u_swirlBoost")
			};
			const quadBuffer = gl.createBuffer();
			gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
			gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
				-1,
				-1,
				1,
				-1,
				-1,
				1,
				1,
				1
			]), gl.STATIC_DRAW);
			const bindQuad = (program) => {
				const position = gl.getAttribLocation(program, "a_position");
				gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
				gl.enableVertexAttribArray(position);
				gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
			};
			const makeTarget = (width, height, initial) => {
				const tex = gl.createTexture();
				if (tex === null) throw new Error("ui-aqua fluid: texture allocation failed");
				gl.bindTexture(gl.TEXTURE_2D, tex);
				if (initial !== void 0) gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, initial);
				else gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
				gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
				gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
				gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
				gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
				const fbo = gl.createFramebuffer();
				gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
				gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
				gl.bindFramebuffer(gl.FRAMEBUFFER, null);
				return {
					fbo,
					tex
				};
			};
			let width = 0;
			let height = 0;
			let flowWidth = 0;
			let flowHeight = 0;
			let flip = false;
			let current = { ...params };
			const pointer = {
				x: .5,
				y: .5,
				smoothX: .5,
				smoothY: .5,
				vx: 0,
				vy: 0,
				svx: 0,
				svy: 0
			};
			const dprCap = Math.min(window.devicePixelRatio || 1, 1.5);
			width = Math.round(canvas.clientWidth * dprCap);
			height = Math.round(canvas.clientHeight * dprCap);
			canvas.width = width;
			canvas.height = height;
			flowWidth = Math.round(width / 4);
			flowHeight = Math.round(height / 4);
			const initial = new Uint8Array(flowWidth * flowHeight * 4);
			for (let i = 0; i < flowWidth * flowHeight; i += 1) {
				initial[4 * i] = 0;
				initial[4 * i + 1] = 128;
				initial[4 * i + 2] = 128;
				initial[4 * i + 3] = 255;
			}
			let targetA = makeTarget(flowWidth, flowHeight, initial);
			let targetB = makeTarget(flowWidth, flowHeight, initial);
			const coarse = window.matchMedia("(hover: none), (pointer: coarse)").matches;
			const ua = navigator;
			const windows = ua.userAgentData ? ua.userAgentData.platform === "Windows" : navigator.userAgent.includes("Windows");
			const onMouseMove = (event) => {
				const rect = canvas.getBoundingClientRect();
				pointer.x = (event.clientX - rect.left) / rect.width;
				pointer.y = 1 - (event.clientY - rect.top) / rect.height;
			};
			if (!coarse && !windows) window.addEventListener("mousemove", onMouseMove);
			const start = performance.now();
			let raf = 0;
			let previous = 0;
			const step = 1e3 / 30;
			const frame = (now) => {
				raf = requestAnimationFrame(frame);
				if (now - previous < step) return;
				previous = now - (now - previous) % step;
				const ratio = Math.min(window.devicePixelRatio || 1, 1.5);
				const nextWidth = Math.round(canvas.clientWidth * ratio);
				const nextHeight = Math.round(canvas.clientHeight * ratio);
				if (nextWidth !== width || nextHeight !== height) {
					width = nextWidth;
					height = nextHeight;
					canvas.width = width;
					canvas.height = height;
				}
				const p = current;
				const s = pointer;
				s.svx *= .94;
				s.svy *= .94;
				s.smoothX += (s.x - s.smoothX) * .12;
				s.smoothY += (s.y - s.smoothY) * .12;
				s.svx += ((s.x - s.smoothX) * .5 - s.svx) * .15;
				s.svy += ((s.y - s.smoothY) * .5 - s.svy) * .15;
				const read = flip ? targetA : targetB;
				const write = flip ? targetB : targetA;
				flip = !flip;
				gl.bindFramebuffer(gl.FRAMEBUFFER, write.fbo);
				gl.viewport(0, 0, flowWidth, flowHeight);
				gl.useProgram(flowProgram);
				bindQuad(flowProgram);
				gl.activeTexture(gl.TEXTURE0);
				gl.bindTexture(gl.TEXTURE_2D, read.tex);
				gl.uniform1i(flow.prev, 0);
				gl.uniform2f(flow.mouse, s.smoothX, s.smoothY);
				gl.uniform2f(flow.velocity, s.svx, s.svy);
				gl.uniform1f(flow.brushRadius, p.mouseRadius);
				gl.uniform1f(flow.brushStrength, p.mouseStrength);
				gl.uniform1f(flow.decay, p.decay);
				gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
				gl.bindFramebuffer(gl.FRAMEBUFFER, null);
				gl.viewport(0, 0, width, height);
				gl.useProgram(displayProgram);
				bindQuad(displayProgram);
				gl.activeTexture(gl.TEXTURE0);
				gl.bindTexture(gl.TEXTURE_2D, write.tex);
				gl.uniform1i(display.flowmap, 0);
				const time = (performance.now() - start) * .001 * (p.speed / 100);
				gl.uniform1f(display.time, time);
				gl.uniform1f(display.pixelRatio, window.devicePixelRatio || 1);
				gl.uniform2f(display.resolution, width, height);
				gl.uniform1f(display.scale, p.scale);
				gl.uniform1f(display.rotation, p.rotation / 90);
				gl.uniform2f(display.offset, p.offsetX / 100, p.offsetY / 100);
				const c1 = hexToRgb(p.color1 || "#2E58A4");
				const c2 = hexToRgb(p.color2 || "#D2E2EE");
				const c3 = hexToRgb(p.color3 || "#FFFFFF");
				gl.uniform4f(display.color1, c1[0], c1[1], c1[2], 1);
				gl.uniform4f(display.color2, c2[0], c2[1], c2[2], 1);
				gl.uniform4f(display.color3, c3[0], c3[1], c3[2], 1);
				gl.uniform1f(display.colorCount, 3);
				gl.uniform1f(display.proportion, p.proportion / 100);
				gl.uniform1f(display.softness, p.softness / 100);
				gl.uniform1f(display.shape, 0);
				gl.uniform1f(display.shapeScale, p.shapeScale / 100);
				gl.uniform1f(display.distortion, p.distortion / 100);
				gl.uniform1f(display.swirl, p.swirl / 50);
				gl.uniform1f(display.swirlIterations, p.swirlIterations);
				gl.uniform1f(display.distortBoost, p.distortBoost);
				gl.uniform1f(display.noiseBoost, p.noiseBoost);
				gl.uniform1f(display.swirlBoost, p.swirlBoost);
				gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
			};
			const handle = {
				setParams: (next) => {
					current = { ...next };
				},
				stir: (x, y, vx, vy) => {
					pointer.x += (x - pointer.x) * .35;
					pointer.y += (y - pointer.y) * .35;
					pointer.svx += (vx - pointer.svx) * .3;
					pointer.svy += (vy - pointer.svy) * .3;
				},
				dispose: () => {
					cancelAnimationFrame(raf);
					window.removeEventListener("mousemove", onMouseMove);
				}
			};
			if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
				frame(performance.now());
				cancelAnimationFrame(raf);
				return handle;
			}
			raf = requestAnimationFrame(frame);
			return handle;
		}
		//#endregion
		//#region src/client/fluid-tones.ts
		/** hsl(h, s, l) → #rrggbb. */
		function hsl(h, s, l) {
			const c = (1 - Math.abs(2 * l - 1)) * s;
			const x = c * (1 - Math.abs(h / 60 % 2 - 1));
			const m = l - c / 2;
			let r = 0;
			let g = 0;
			let b = 0;
			if (h < 60) {
				r = c;
				g = x;
			} else if (h < 120) {
				r = x;
				g = c;
			} else if (h < 180) {
				g = c;
				b = x;
			} else if (h < 240) {
				g = x;
				b = c;
			} else if (h < 300) {
				r = x;
				b = c;
			} else {
				r = c;
				b = x;
			}
			const toHex = (v) => Math.round((v + m) * 255).toString(16).padStart(2, "0");
			return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
		}
		/**
		* Palette for the given hue (0-360) and depth (0-100), per scheme.
		* The depth ramp is piecewise: the lower half sweeps from the absolute
		* extreme — pure black in dark mode, the deep saturated shade (e.g. #8B0000
		* for red) in light mode — up to the shipped mid look; the upper half
		* sweeps from mid to pale (#FFCCCB for red). Stepless HSL interpolation.
		*/
		function fluidToneColors(dark, hue, depth) {
			const h = ((hue + 217) % 360 + 360) % 360;
			const d = Math.min(1, Math.max(0, depth / 100));
			const ramp = (deep, mid, pale) => d < .5 ? deep + (mid - deep) * d / .5 : mid + (pale - mid) * (d - .5) / .5;
			if (dark) return {
				color1: hsl(h, .85, ramp(0, .46, .62)),
				color2: hsl(h, .9, ramp(0, .305, .45)),
				color3: hsl(h, .5, ramp(0, .075, .1))
			};
			return {
				color1: hsl(h, 1, ramp(.27, .45, .9)),
				color2: hsl(h, .55, .86),
				color3: hsl(h, .25, .955)
			};
		}
		//#endregion
		//#region src/client/fluid-interactions.ts
		/** Normalized shader-space coordinates for one canvas. */
		function uv(canvas, clientX, clientY) {
			const rect = canvas.getBoundingClientRect();
			return {
				x: rect.width <= 0 ? .5 : (clientX - rect.left) / rect.width,
				y: rect.height <= 0 ? .5 : 1 - (clientY - rect.top) / rect.height
			};
		}
		/**
		* Attach the button ripple listeners.
		* @param targets - the fluid handle and its canvas.
		* @returns disposer removing every listener.
		*/
		function attachFluidInteractions(targets) {
			const { main, mainCanvas } = targets;
			const lastStir = /* @__PURE__ */ new WeakMap();
			const ripples = /* @__PURE__ */ new Set();
			const stirButton = (button, strength) => {
				const now = performance.now();
				if (now - (lastStir.get(button) ?? 0) < 160) return;
				lastStir.set(button, now);
				const rect = button.getBoundingClientRect();
				const point = uv(mainCanvas, rect.left + rect.width / 2, rect.top + rect.height / 2);
				main.stir(point.x, point.y, 0, -strength);
			};
			/** Slow radial ripple: a ring of gentle outward stirs expanding from the
			*  click point. Radius eases from zero so the influence creeps outward. */
			const ripple = (cx, cy) => {
				const rect = mainCanvas.getBoundingClientRect();
				if (rect.width <= 0 || rect.height <= 0) return;
				const ux = (cx - rect.left) / rect.width;
				const uy = 1 - (cy - rect.top) / rect.height;
				const start = performance.now();
				const duration = 1500;
				const maxRadius = 120;
				const count = 8;
				const step = () => {
					const t = performance.now() - start;
					if (t > duration) return;
					const k = t / duration;
					const radius = maxRadius * k * k;
					const strength = .05 * (1 - k);
					const spin = .4 * k;
					for (let i = 0; i < count; i += 1) {
						const angle = i / count * Math.PI * 2 + spin;
						const px = ux + radius * Math.cos(angle) / rect.width;
						const py = uy + radius * Math.sin(angle) / rect.height;
						main.stir(px, py, Math.cos(angle) * strength, -Math.sin(angle) * strength);
					}
					const id = requestAnimationFrame(step);
					ripples.add(id);
				};
				const id = requestAnimationFrame(step);
				ripples.add(id);
			};
			const onPointerOver = (event) => {
				const button = event.target?.closest?.("button");
				if (button !== void 0 && button !== null) stirButton(button, .04);
			};
			const onClick = (event) => {
				const button = event.target?.closest?.("button");
				if (button === void 0 || button === null) return;
				const now = performance.now();
				if (now - (lastStir.get(button) ?? 0) < 500) return;
				lastStir.set(button, now);
				const rect = button.getBoundingClientRect();
				ripple(rect.left + rect.width / 2, rect.top + rect.height / 2);
			};
			document.addEventListener("pointerover", onPointerOver, { capture: true });
			document.addEventListener("click", onClick, { capture: true });
			return () => {
				for (const id of ripples) cancelAnimationFrame(id);
				ripples.clear();
				document.removeEventListener("pointerover", onPointerOver, { capture: true });
				document.removeEventListener("click", onClick, { capture: true });
			};
		}
		//#endregion
		//#region src/client/seam-stamper.ts
		const SEAMS = [
			{
				attribute: "data-dsh-frame",
				selector: ":has(> [class*=\"sidebarCol\"])"
			},
			{
				attribute: "data-dsh-sidebar-root",
				selector: "[class*=\"sidebarCol\"] [class*=\"root\"]",
				first: true
			},
			{
				attribute: "data-dsh-surface",
				selector: "button[class*=\"newSession\"]"
			},
			{
				attribute: "data-dsh-trajectory",
				selector: "[data-conversation-composer-overlay]"
			},
			{
				attribute: "data-dsh-details",
				selector: "[class*=\"detailsCol\"] [class*=\"root\"]",
				first: true
			},
			{
				attribute: "data-dsh-inputbar",
				selector: ":has(> [data-composer-card])"
			},
			{
				attribute: "data-dsh-add",
				selector: "[data-composer-card] [class*=\"add\"]"
			},
			{
				attribute: "data-dsh-stats",
				selector: "[data-slot=\"conversation.composer.dock\"] [class*=\"root\"]"
			},
			{
				attribute: "data-dsh-aqua-spot",
				selector: "[data-phase] > header",
				first: true
			},
			{
				attribute: "data-dsh-aqua-spot",
				selector: "[class*=\"sidebarCol\"]",
				first: true
			},
			{
				attribute: "data-dsh-aqua-spot",
				selector: "[data-dsh-inputbar]"
			},
			{
				attribute: "data-dsh-aqua-spot",
				selector: "[data-dsh-trajectory]"
			},
			{
				attribute: "data-dsh-aqua-spot",
				selector: "[data-dsh-surface]"
			},
			{
				attribute: "data-dsh-wordmark",
				selector: "[class*=\"sidebarCol\"] [class*=\"brand\"]:not(img)",
				first: true
			}
		];
		function stamp(seam) {
			if (seam.first) {
				const el = document.querySelector(seam.selector);
				if (el !== null && !el.hasAttribute(seam.attribute)) el.setAttribute(seam.attribute, "");
				return;
			}
			for (const el of document.querySelectorAll(seam.selector)) if (!el.hasAttribute(seam.attribute)) el.setAttribute(seam.attribute, "");
		}
		function stampAll() {
			for (const seam of SEAMS) stamp(seam);
		}
		/**
		* Stamp the seams once, then keep them stamped as React remounts nodes.
		* @returns a disposer that disconnects the observer.
		*/
		function startSeamStamper() {
			stampAll();
			const observer = new MutationObserver(() => {
				stampAll();
			});
			observer.observe(document.documentElement, {
				childList: true,
				subtree: true
			});
			return () => {
				observer.disconnect();
			};
		}
		//#endregion
		//#region src/client/orion-brand-policy.ts
		// Upstream particle-brand renderer removed by ORION brand policy.
		//#endregion
		//#region src/client/mesh.ts
		/**
		* Interactive mesh: the deepseek.com/harness hero's dot-grid decoration —
		* a 90px grid of dots with spring physics that repel from the pointer
		* (radius 140px), the grid lines stretching with them. Faithful port of the
		* site's `h()` grid component (30fps, dpr ≤ 2, idle-pause). Rendered inside
		* the ambient scene behind the app content; pointer-events pass through.
		*/
		const SPACING = 90;
		const REPEL_RADIUS = 140;
		const REPEL_FORCE = 30;
		const SPRING = .05;
		const DAMPING = .85;
		const LINE_GAP = 10;
		const MIN_LINE_DIST = 20;
		const LINE_COLOR = "rgba(60, 100, 160, ";
		const DOT_COLOR = "rgba(60, 100, 160, ";
		const LINE_ALPHA = .1;
		const DOT_ALPHA = .2;
		const FPS = 30;
		/**
		* Mount the interactive mesh into `host` (the ambient scene).
		* @param host - the container the mesh canvas is appended to.
		* @returns the handle.
		*/
		function mountMesh(host) {
			const canvas = document.createElement("canvas");
			canvas.setAttribute("data-dsh-aqua-mesh", "");
			canvas.setAttribute("aria-hidden", "true");
			host.appendChild(canvas);
			const ctx = canvas.getContext("2d");
			if (ctx === null) {
				canvas.remove();
				return { dispose: () => {} };
			}
			const reduced = typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches;
			const coarse = typeof matchMedia !== "undefined" && matchMedia("(hover: none), (pointer: coarse)").matches;
			const dpr = Math.min(window.devicePixelRatio || 1, 2);
			let dots = [];
			let cols = 0;
			let rows = 0;
			let w = 0;
			let h = 0;
			let raf = 0;
			let disposed = false;
			let idle = false;
			let visible = true;
			let resizeTimer = 0;
			const mouse = {
				x: NaN,
				y: NaN
			};
			const build = () => {
				cols = Math.ceil(w / SPACING) + 1;
				rows = Math.ceil(h / SPACING) + 1;
				const startX = (w - (cols - 1) * SPACING) / 2;
				const startY = (h - (rows - 1) * SPACING) / 2;
				dots = [];
				for (let ry = 0; ry < rows; ry++) for (let rx = 0; rx < cols; rx++) {
					const x = startX + SPACING * rx;
					const y = startY + SPACING * ry;
					dots.push({
						restX: x,
						restY: y,
						x,
						y,
						vx: 0,
						vy: 0
					});
				}
			};
			const resize = () => {
				const cw = canvas.clientWidth;
				const ch = canvas.clientHeight;
				if (cw === w && ch === h) return;
				w = cw;
				h = ch;
				canvas.width = Math.max(1, Math.round(w * dpr));
				canvas.height = Math.max(1, Math.round(h * dpr));
				ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
				window.clearTimeout(resizeTimer);
				resizeTimer = window.setTimeout(build, 150);
			};
			resize();
			build();
			const wake = () => {
				if (!idle) return;
				idle = false;
				if (raf === 0) raf = requestAnimationFrame(frame);
			};
			const onMove = (event) => {
				if (reduced || coarse) return;
				mouse.x = event.clientX;
				mouse.y = event.clientY;
				wake();
			};
			if (!reduced && !coarse) window.addEventListener("pointermove", onMove, { passive: true });
			let last = 0;
			const frame = (now) => {
				raf = 0;
				if (disposed) return;
				if (!visible || now - last < 1e3 / FPS) {
					raf = requestAnimationFrame(frame);
					return;
				}
				last = now - (now - last) % (1e3 / FPS);
				const cw = canvas.clientWidth;
				const ch = canvas.clientHeight;
				if (cw !== w || ch !== h) resize();
				ctx.clearRect(0, 0, w, h);
				const mx = mouse.x;
				const my = mouse.y;
				let maxV = 0;
				for (const dot of dots) {
					if (!Number.isNaN(mx) && !Number.isNaN(my)) {
						const dx = dot.x - mx;
						const dy = dot.y - my;
						const dist = Math.sqrt(dx * dx + dy * dy);
						if (dist < REPEL_RADIUS && dist > .1) {
							const force = (1 - dist / REPEL_RADIUS) * REPEL_FORCE;
							const nx = dx / dist;
							const ny = dy / dist;
							dot.vx += nx * force * .1;
							dot.vy += ny * force * .1;
						}
					}
					const sx = dot.restX - dot.x;
					const sy = dot.restY - dot.y;
					dot.vx += SPRING * sx;
					dot.vy += SPRING * sy;
					dot.vx *= DAMPING;
					dot.vy *= DAMPING;
					dot.x += dot.vx;
					dot.y += dot.vy;
					const v = Math.abs(dot.vx) + Math.abs(dot.vy);
					if (v > maxV) maxV = v;
				}
				ctx.strokeStyle = `${LINE_COLOR}${LINE_ALPHA})`;
				ctx.lineWidth = .5;
				for (let ry = 0; ry < rows; ry++) for (let rx = 0; rx < cols - 1; rx++) {
					const a = dots[ry * cols + rx];
					const b = dots[ry * cols + rx + 1];
					const dx = b.x - a.x;
					const dy = b.y - a.y;
					const dist = Math.sqrt(dx * dx + dy * dy);
					if (dist < MIN_LINE_DIST) continue;
					const ux = dx / dist;
					const uy = dy / dist;
					ctx.beginPath();
					ctx.moveTo(a.x + LINE_GAP * ux, a.y + LINE_GAP * uy);
					ctx.lineTo(b.x - LINE_GAP * ux, b.y - LINE_GAP * uy);
					ctx.stroke();
				}
				for (let ry = 0; ry < rows - 1; ry++) for (let rx = 0; rx < cols; rx++) {
					const a = dots[ry * cols + rx];
					const b = dots[(ry + 1) * cols + rx];
					const dx = b.x - a.x;
					const dy = b.y - a.y;
					const dist = Math.sqrt(dx * dx + dy * dy);
					if (dist < MIN_LINE_DIST) continue;
					const ux = dx / dist;
					const uy = dy / dist;
					ctx.beginPath();
					ctx.moveTo(a.x + LINE_GAP * ux, a.y + LINE_GAP * uy);
					ctx.lineTo(b.x - LINE_GAP * ux, b.y - LINE_GAP * uy);
					ctx.stroke();
				}
				ctx.fillStyle = `${DOT_COLOR}${DOT_ALPHA})`;
				for (const dot of dots) {
					let r = 1.8;
					let alpha = DOT_ALPHA;
					if (!Number.isNaN(mx) && !Number.isNaN(my)) {
						const dx = dot.x - mx;
						const dy = dot.y - my;
						const dist = Math.sqrt(dx * dx + dy * dy);
						const near = Math.max(0, 1 - dist / REPEL_RADIUS);
						r = 1.8 + 2 * near;
						alpha = DOT_ALPHA + .4 * near;
					}
					ctx.globalAlpha = alpha;
					const size = 2 * r;
					ctx.fillRect(dot.x - r, dot.y - r, size, size);
				}
				ctx.globalAlpha = 1;
				if (maxV < .01) idle = true;
				else raf = requestAnimationFrame(frame);
			};
			if (reduced || coarse) {
				resize();
				ctx.clearRect(0, 0, w, h);
				ctx.strokeStyle = `${LINE_COLOR}${LINE_ALPHA})`;
				ctx.lineWidth = .5;
				for (let ry = 0; ry < rows; ry++) for (let rx = 0; rx < cols - 1; rx++) {
					const a = dots[ry * cols + rx];
					const b = dots[ry * cols + rx + 1];
					ctx.beginPath();
					ctx.moveTo(a.x + LINE_GAP, a.y);
					ctx.lineTo(b.x - LINE_GAP, b.y);
					ctx.stroke();
				}
				for (let ry = 0; ry < rows - 1; ry++) for (let rx = 0; rx < cols; rx++) {
					const a = dots[ry * cols + rx];
					const b = dots[(ry + 1) * cols + rx];
					ctx.beginPath();
					ctx.moveTo(a.x, a.y + LINE_GAP);
					ctx.lineTo(b.x, b.y - LINE_GAP);
					ctx.stroke();
				}
				ctx.fillStyle = `${DOT_COLOR}${DOT_ALPHA})`;
				for (const dot of dots) ctx.fillRect(dot.x - 1.8, dot.y - 1.8, 3.6, 3.6);
			} else {
				raf = requestAnimationFrame(frame);
				const observer = new IntersectionObserver(([entry]) => {
					visible = entry.isIntersecting;
					if (visible) wake();
				}, { threshold: 0 });
				observer.observe(canvas);
				return { dispose: () => {
					disposed = true;
					cancelAnimationFrame(raf);
					window.clearTimeout(resizeTimer);
					observer.disconnect();
					window.removeEventListener("pointermove", onMove);
					canvas.remove();
				} };
			}
			return { dispose: () => {
				disposed = true;
				cancelAnimationFrame(raf);
				window.clearTimeout(resizeTimer);
				window.removeEventListener("pointermove", onMove);
				canvas.remove();
			} };
		}
		//#endregion
		//#region src/client/spot-core.ts
		/**
		* Spot geometry + overlay maintenance, shared by the spotlight/tilt
		* controller (spotlight.ts).
		*
		* A "spot" is a floating-glass pane stamped with `data-dsh-aqua-spot` by the
		* seam-stamper. One injected overlay lives inside a spot:
		* `data-dsh-aqua-glow` — the cursor glow surface (geometry set by the hover
		* controller; the radial fill lives in the stylesheet). It is re-attached
		* after React re-renders wipe it (one shared MutationObserver).
		*/
		/** Seam attribute marking a floating-glass pane as a spotlight target. */
		const SPOT_ATTR = "data-dsh-aqua-spot";
		/** Attribute on the injected glow overlay div. */
		const GLOW_ATTR = "data-dsh-aqua-glow";
		/** Marker set on a pane while the pointer is inside it. */
		const ON_ATTR = "data-spot-on";
		/** Selector matching every stamped pane. */
		const SPOT_SELECTOR = `[${SPOT_ATTR}]`;
		/** Nearest stamped pane from an event target (null when outside all panes). */
		function closestSpot(target) {
			return target instanceof Element ? target.closest(SPOT_SELECTOR) : null;
		}
		/** Every stamped pane in document order. */
		function spotElements() {
			return Array.from(document.querySelectorAll(SPOT_SELECTOR));
		}
		/**
		* The visible glass region of a pane (viewport rect). The fused
		* composer+stats spot is the wider invisible inputbar wrapper — its glass is
		* the union of the composer card and the docked stats band, so the wrapper's
		* side gutters stay outside every effect.
		*/
		function visualRect(spot) {
			if (spot.querySelector("[data-composer-card]") !== null) {
				const r0 = spot.querySelector("[data-composer-card]").getBoundingClientRect();
				const stats = spot.querySelector("[data-dsh-stats]");
				if (stats === null) return r0;
				const r1 = stats.getBoundingClientRect();
				const left = Math.min(r0.left, r1.left);
				const top = Math.min(r0.top, r1.top);
				return new DOMRect(left, top, Math.max(r0.right, r1.right) - left, Math.max(r0.bottom, r1.bottom) - top);
			}
			return spot.getBoundingClientRect();
		}
		/** Is the pointer over the visible glass of the pane? */
		function inside(visual, clientX, clientY) {
			return clientX >= visual.left && clientX <= visual.right && clientY >= visual.top && clientY <= visual.bottom;
		}
		/** Offset-chain position of `el` within `ancestor` (both boxes), in the
		*  UNTRANSFORMED layout space — offsetLeft/offsetTop ignore transforms, so
		*  this stays exact while the pane is tilted. */
		function localTopLeft(el, ancestor) {
			let x = 0;
			let y = 0;
			let node = el;
			while (node !== null && node !== ancestor) {
				x += node.offsetLeft;
				y += node.offsetTop;
				node = node.offsetParent;
			}
			return {
				x,
				y
			};
		}
		/**
		* The visible glass region of a pane in the pane's own local space
		* (untransformed — safe to measure while tilted). For the fused
		* composer+stats spot this is the union of the composer card and the docked
		* stats band; for the other panes it is the pane's own box.
		*/
		function glassLocalRect(spot) {
			const card = spot.querySelector("[data-composer-card]");
			if (card === null) return {
				left: 0,
				top: 0,
				width: spot.offsetWidth,
				height: spot.offsetHeight
			};
			const cardPos = localTopLeft(card, spot);
			let left = cardPos.x;
			let top = cardPos.y;
			let right = left + card.offsetWidth;
			let bottom = top + card.offsetHeight;
			const stats = spot.querySelector("[data-dsh-stats]");
			if (stats !== null) {
				const statsPos = localTopLeft(stats, spot);
				left = Math.min(left, statsPos.x);
				top = Math.min(top, statsPos.y);
				right = Math.max(right, statsPos.x + stats.offsetWidth);
				bottom = Math.max(bottom, statsPos.y + stats.offsetHeight);
			}
			return {
				left,
				top,
				width: right - left,
				height: bottom - top
			};
		}
		/** Ensure the pane carries exactly one glow overlay div. */
		function ensureGlow(spot) {
			let glow = spot.querySelector(`:scope > [${GLOW_ATTR}]`);
			if (glow === null) {
				glow = document.createElement("div");
				glow.setAttribute(GLOW_ATTR, "");
				glow.setAttribute("aria-hidden", "true");
				spot.appendChild(glow);
			}
			return glow;
		}
		/**
		* One shared observer + resize feed: keeps the glow divs glued to the panes
		* through React re-renders and notifies the caller of DOM/layout changes
		* (the caller coalesces the callbacks).
		* @returns a disposer that removes every injected glow div.
		*/
		function startOverlayKeeper(onChange) {
			const tick = () => {
				for (const spot of spotElements()) ensureGlow(spot);
				onChange();
			};
			tick();
			const observer = new MutationObserver(tick);
			observer.observe(document.documentElement, {
				childList: true,
				subtree: true
			});
			window.addEventListener("resize", tick, { passive: true });
			return () => {
				observer.disconnect();
				window.removeEventListener("resize", tick);
				for (const glow of document.querySelectorAll(`[${GLOW_ATTR}]`)) glow.remove();
			};
		}
		//#endregion
		//#region src/client/spotlight.ts
		/**
		* Cursor spotlight glow + geometric tilt: the deepseek.com/harness
		* feature-card hover interactions, ported onto the floating glass panes.
		*
		* Two effects ride the same hover marker (`data-spot-on`):
		* - a blue radial glow that follows the cursor — a `data-dsh-aqua-glow`
		*   overlay inside each pane whose inline background a JS pointermove
		*   writes (`radial-gradient(180px at Xpx Ypx, rgba(120,170,255,.15),
		*   transparent 70%)`, official values). The glow sits BEHIND the glass
		*   (z-index -1) so it diffuses through the translucent surface and never
		*   covers content;
		* - a cursor-driven rigid tilt written inline per pointermove, the official
		*   card's exact recipe (sign-verified from its inline transform):
		*   `perspective(800px) rotateX(θx) rotateY(θy) scale(1.01)` with
		*   θx = −k·Δy, θy = +k·Δx — the edge under the cursor sinks, the far edge
		*   lifts (cursor right ⇒ right sinks; cursor top ⇒ top sinks), ≈1° at the
		*   pane edge, 0.1s ease-out transition;
		*
		* Port notes:
		* - the sidebar NEVER tilts (its settings overlay renders inside the column
		*   and a running transform would re-anchor it — the panel traps at the
		*   column width); it keeps the glow;
		* - the fused composer+stats spot is the wider invisible inputbar wrapper:
		*   the hover region, glow geometry and tilt pivot are computed against the
		*   VISIBLE glass (see visualRect / glassLocalRect in spot-core.ts), so the
		*   wrapper's side gutters never respond;
		* - the tilt rides a short CSS transition and reduced motion skips it;
		* - geometry is measured ONCE per hover session in untransformed local space
		*   (offset-based — immune to the pane's own rotation) and refreshed on
		*   DOM/layout changes, so the per-frame path does zero layout reads.
		*
		* Two html-attribute gates from the layer's settings: `data-dsh-aqua-spotlight`
		* (glow) and `data-dsh-aqua-press` (tilt). Hover tracking runs when EITHER is
		* on. The glow divs are maintained by spot-core's overlay keeper, independent
		* of the toggles.
		*/
		/** html attribute the layer uses to switch the glow effect (its toggle). */
		const SPOTLIGHT_ATTRIBUTE = "data-dsh-aqua-spotlight";
		/** html attribute the layer uses to switch the tilt effect (its toggle). */
		const PRESS_ATTRIBUTE = "data-dsh-aqua-press";
		/** Glow radius, px — matches the official card. */
		const GLOW_RADIUS = 180;
		/** Fallback glow color (the CSS var is normally provided by the stylesheet). */
		const GLOW_FALLBACK = "rgba(90, 215, 255, 0.17)";
		/** Tilt magnitude at the pane edge, radians (≈1° — perceptible but gentle). */
		const TILT_MAX = .0175;
		/** Tilt perspective distance, px (official value). */
		const TILT_PERSPECTIVE = 800;
		/** Ease-back settle time (ms) — must outlast the CSS transform transition. */
		const SETTLE_MS = 240;
		/** The glow is live only while its gate attribute is on <html>. */
		function glowGated() {
			return document.documentElement.hasAttribute(SPOTLIGHT_ATTRIBUTE);
		}
		/** The tilt is live only while its gate attribute is on <html>. */
		function tiltGated() {
			return document.documentElement.hasAttribute(PRESS_ATTRIBUTE);
		}
		/** Hover tracking runs when EITHER effect is enabled. */
		function hoverGated() {
			return glowGated() || tiltGated();
		}
		/** Whether the tilt may run on this pane right now. */
		function tiltable(spot) {
			if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
			if (spot.matches("[class*=\"sidebarCol\"]") && document.querySelector("[role=\"dialog\"]") !== null) return false;
			return true;
		}
		/**
		* Attach the delegated pointer feeds. Everything is document-level: no
		* per-pane listeners, and the rAF merge collapses pointermove bursts to one
		* style write per frame.
		* @returns a disposer that drops listeners, overlays, and inline styles.
		*/
		function startSpotlight() {
			/** The hovered pane (cleared on leave). */
			let current = null;
			/** Geometry for the hovered pane. */
			let session = null;
			let raf = 0;
			let refreshRaf = 0;
			/** Panes currently carrying a JS-written transform (wipe only those). */
			const tilted = /* @__PURE__ */ new WeakSet();
			/** Pending ease-back removal timers per pane (leave → neutral → cleanup). */
			const settle = /* @__PURE__ */ new Map();
			/** Ease a pressed pane back to neutral, then drop the inline transform. */
			const easeBack = (spot) => {
				if (!tilted.has(spot)) return;
				tilted.delete(spot);
				spot.style.transform = `perspective(${TILT_PERSPECTIVE}px) rotateX(0rad) rotateY(0rad) scale(1)`;
				const id = window.setTimeout(() => {
					settle.delete(spot);
					spot.style.removeProperty("transform");
					spot.style.removeProperty("transform-origin");
				}, SETTLE_MS);
				settle.set(spot, id);
			};
			/** Drop every effect this controller wrote onto a pane. */
			const clearSpot = (spot) => {
				spot.removeAttribute(ON_ATTR);
				if (current === spot) {
					current = null;
					session = null;
				}
				const glow = spot.querySelector(`:scope > [${GLOW_ATTR}]`);
				if (glow !== null) glow.style.removeProperty("background-image");
				easeBack(spot);
			};
			/** Capture (or refresh) the hover geometry; sets the glow overlay box. */
			const measure = (spot) => {
				const visual = visualRect(spot);
				const local = glassLocalRect(spot);
				const glow = glowGated() ? ensureGlow(spot) : null;
				if (glow !== null) {
					glow.style.left = `${local.left}px`;
					glow.style.top = `${local.top}px`;
					glow.style.width = `${local.width}px`;
					glow.style.height = `${local.height}px`;
				}
				return {
					spot,
					visual,
					local,
					glow
				};
			};
			/** Write the glow gradient and/or the tilt transform for the pointer position. */
			const paint = (s, clientX, clientY) => {
				if (raf !== 0) return;
				raf = requestAnimationFrame(() => {
					raf = 0;
					const { spot, visual, local } = s;
					if (!inside(visual, clientX, clientY)) {
						clearSpot(spot);
						return;
					}
					let glow = s.glow;
					if (glow === null && glowGated()) {
						s = session = measure(spot);
						glow = s.glow;
					}
					if (glow !== null) if (glowGated()) glow.style.backgroundImage = `radial-gradient(${GLOW_RADIUS}px at ${clientX - visual.left}px ${clientY - visual.top}px, var(--dsh-aqua-spot-color, ${GLOW_FALLBACK}), transparent 70%)`;
					else glow.style.removeProperty("background-image");
					if (tiltGated() && tiltable(spot)) {
						const dx = Math.min(.5, Math.max(-.5, (clientX - visual.left) / visual.width - .5));
						const dy = Math.min(.5, Math.max(-.5, (clientY - visual.top) / visual.height - .5));
						const tiltMax = spot.hasAttribute("data-dsh-trajectory") ? TILT_MAX * .5 : TILT_MAX;
						spot.style.transformOrigin = `${local.left + local.width / 2}px ${local.top + local.height / 2}px`;
						spot.style.transform = `perspective(${TILT_PERSPECTIVE}px) rotateX(${tiltMax * -2 * dy}rad) rotateY(${tiltMax * 2 * dx}rad) scale(1.01)`;
						tilted.add(spot);
					} else if (tilted.has(spot)) easeBack(spot);
				});
			};
			const onMove = (event) => {
				if (!hoverGated()) return;
				const spot = closestSpot(event.target);
				if (spot === null || session?.spot !== spot) return;
				paint(session, event.clientX, event.clientY);
			};
			const onOver = (event) => {
				if (!hoverGated()) return;
				const spot = closestSpot(event.target);
				if (spot === null) return;
				if (spot.matches("[class*=\"sidebarCol\"]") && document.querySelector("[role=\"dialog\"]") !== null) return;
				const next = measure(spot);
				if (!inside(next.visual, event.clientX, event.clientY)) return;
				const id = settle.get(spot);
				if (id !== void 0) {
					clearTimeout(id);
					settle.delete(spot);
				}
				spot.setAttribute(ON_ATTR, "");
				current = spot;
				session = next;
				paint(next, event.clientX, event.clientY);
			};
			const onOut = (event) => {
				const spot = closestSpot(event.target);
				if (spot === null || spot !== current) return;
				if (session !== null && inside(session.visual, event.clientX, event.clientY)) return;
				clearSpot(spot);
			};
			const keeper = startOverlayKeeper(() => {
				for (const spot of spotElements()) {
					if (!spot.matches("[class*=\"sidebarCol\"]")) continue;
					if (spot.querySelector("[role=\"dialog\"]") === null) continue;
					spot.removeAttribute(ON_ATTR);
					const id = settle.get(spot);
					if (id !== void 0) {
						clearTimeout(id);
						settle.delete(spot);
					}
					tilted.delete(spot);
					spot.style.setProperty("transition", "none");
					spot.style.removeProperty("transform");
					spot.style.removeProperty("transform-origin");
					spot.offsetWidth;
					spot.style.removeProperty("transition");
					if (current === spot) {
						current = null;
						session = null;
					}
				}
				if (session === null || refreshRaf !== 0) return;
				refreshRaf = requestAnimationFrame(() => {
					refreshRaf = 0;
					if (session !== null) session = measure(session.spot);
				});
			});
			document.addEventListener("pointermove", onMove, { passive: true });
			document.addEventListener("pointerover", onOver, { passive: true });
			document.addEventListener("pointerout", onOut, { passive: true });
			return () => {
				document.removeEventListener("pointermove", onMove);
				document.removeEventListener("pointerover", onOver);
				document.removeEventListener("pointerout", onOut);
				keeper();
				if (raf !== 0) cancelAnimationFrame(raf);
				if (refreshRaf !== 0) cancelAnimationFrame(refreshRaf);
				for (const id of settle.values()) clearTimeout(id);
				settle.clear();
				for (const spot of spotElements()) {
					spot.removeAttribute(ON_ATTR);
					if (tilted.has(spot)) {
						tilted.delete(spot);
						spot.style.removeProperty("transform");
						spot.style.removeProperty("transform-origin");
					}
				}
			};
		}
		//#endregion
		//#region src/client/theme-layer.ts
		/** html attribute selecting the Aqua layer: CSS hooks and ambient effects. */
		const AQUA_ATTRIBUTE = "data-dsh-aqua";
		/** localStorage key carrying the layer enable flag. */
		const AQUA_ENABLED_KEY = "dsh.ui-aqua.enabled";
		/** The layer's identity in the theme override stack (inspection-visible). */
		const OVERRIDE_SOURCE = "@deepseek-ai/dsh-client-ui-aqua";
		const FONT_STACK = "var(--owa-font-stack, sans-serif)";
		/** Scheme-invariant override value (applied to both palettes). */
		const both = (value) => ({
			light: value,
			dark: value
		});
		/**
		* Alias-token override layer: the deep-sea palette. Every value is a
		* `{ light, dark }` pair so the layer stays legible when the user switches
		* the Appearance preference — dark is deep-sea navy, light is cool white-blue.
		*/
		const AQUA_TOKEN_OVERRIDES = {
			"--dsw-font-family": both(FONT_STACK),
			"--dsw-alias-bg-base": {
				light: "#F4F8FD",
				dark: "#0C121B"
			},
			"--dsw-alias-bg-layer-1": {
				light: "#FFFFFF",
				dark: "#111A27"
			},
			"--dsw-alias-bg-layer-2": {
				light: "#ECF2FA",
				dark: "#162130"
			},
			"--dsw-alias-bg-layer-3": {
				light: "#E2EBF7",
				dark: "#1C2A3D"
			},
			"--dsw-alias-bg-overlay": {
				light: "#DCE7F4",
				dark: "#22334A"
			},
			"--dsw-alias-bg-module-platform": {
				light: "#FFFFFF",
				dark: "#111A27"
			},
			"--dsw-alias-bg-multi-select": {
				light: "#FFFFFF",
				dark: "#162130"
			},
			"--dsw-alias-bg-skeleton": {
				light: "rgba(19, 45, 83, 0.08)",
				dark: "rgba(148, 180, 220, 0.12)"
			},
			"--dsw-alias-bg-mask-1": {
				light: "rgba(19, 37, 62, 0.3)",
				dark: "rgba(4, 8, 14, 0.55)"
			},
			"--dsw-alias-bg-mask-2": {
				light: "rgba(19, 37, 62, 0.12)",
				dark: "rgba(4, 8, 14, 0.25)"
			},
			"--dsw-alias-bg-mask-3": {
				light: "rgba(19, 37, 62, 0.3)",
				dark: "rgba(4, 8, 14, 0.5)"
			},
			"--dsw-alias-bg-mask-drop": {
				light: "rgba(244, 248, 253, 0.72)",
				dark: "rgba(12, 18, 27, 0.7)"
			},
			"--dsw-alias-border-l1": {
				light: "rgba(19, 45, 83, 0.08)",
				dark: "rgba(148, 180, 220, 0.08)"
			},
			"--dsw-alias-border-l2": {
				light: "rgba(19, 45, 83, 0.14)",
				dark: "rgba(148, 180, 220, 0.15)"
			},
			"--dsw-alias-border-l2-darkmode-thin": {
				light: "rgba(19, 45, 83, 0.1)",
				dark: "rgba(148, 180, 220, 0.1)"
			},
			"--dsw-alias-border-l3": {
				light: "rgba(19, 45, 83, 0.22)",
				dark: "rgba(148, 180, 220, 0.24)"
			},
			"--dsw-alias-border-l4": {
				light: "rgba(19, 45, 83, 0.32)",
				dark: "rgba(148, 180, 220, 0.34)"
			},
			"--dsw-alias-border-inverted": {
				light: "rgba(19, 45, 83, 0.06)",
				dark: "rgba(148, 180, 220, 0.12)"
			},
			"--dsw-alias-border-inverted2": {
				light: "rgba(19, 45, 83, 0.08)",
				dark: "rgba(148, 180, 220, 0.08)"
			},
			"--dsw-alias-label-primary": {
				light: "#13243E",
				dark: "#EAF2FC"
			},
			"--dsw-alias-label-secondary": {
				light: "#40597A",
				dark: "#AFC3DC"
			},
			"--dsw-alias-label-tertiary": {
				light: "#5D7696",
				dark: "#8399B5"
			},
			"--dsw-alias-label-caption": {
				light: "#7E93AC",
				dark: "#6B829F"
			},
			"--dsw-alias-label-dimmed": {
				light: "#C9D4E2",
				dark: "#4E5F76"
			},
			"--dsw-alias-label-primary-bluish": {
				light: "#2E5EB8",
				dark: "#BFD6F6"
			},
			"--dsw-alias-label-primary-dimmed": {
				light: "#1E3556",
				dark: "#D7E3F4"
			},
			"--dsw-alias-label-primary-inverted": {
				light: "#FFFFFF",
				dark: "#162130"
			},
			"--dsw-alias-label-primary-foreground": {
				light: "#FFFFFF",
				dark: "#FFFFFF"
			},
			"--dsw-alias-brand-primary": {
				light: "#13243E",
				dark: "#EAF2FC"
			},
			"--dsw-alias-brand-text": {
				light: "#13243E",
				dark: "#EAF2FC"
			},
			"--dsw-alias-brand-primary-invert": {
				light: "#FFFFFF",
				dark: "#0C121B"
			},
			"--dsw-alias-brand-primary-new-colorprimary-new-color": {
				light: "#3F76D8",
				dark: "#6E9BE8"
			},
			"--dsw-alias-state-business-primary": {
				light: "#3F76D8",
				dark: "#6E9BE8"
			},
			"--dsw-alias-state-business-tertiary": {
				light: "#DCE9FB",
				dark: "#1D2C44"
			},
			"--dsw-alias-state-success-tertiary": {
				light: "#DDF3E4",
				dark: "#12271C"
			},
			"--dsw-alias-state-warn-tertiary": {
				light: "#FCEED6",
				dark: "#2A2416"
			},
			"--dsw-alias-button-primary-fill": {
				light: "#3F76D8",
				dark: "#4A7FD9"
			},
			"--dsw-alias-button-primary-hover": {
				light: "#5C8DE0",
				dark: "#5E8FE6"
			},
			"--dsw-alias-button-primary-dimmed": {
				light: "#DCE9FB",
				dark: "#162130"
			},
			"--dsw-alias-button-info-fill": {
				light: "#3F76D8",
				dark: "#6E9BE8"
			},
			"--dsw-alias-button-info-hover": {
				light: "#5C8DE0",
				dark: "#7FA8EF"
			},
			"--dsw-alias-button-elevated-fill": {
				light: "#FFFFFF",
				dark: "#162130"
			},
			"--dsw-alias-button-floating-fill": {
				light: "#FFFFFF",
				dark: "#162130"
			},
			"--dsw-alias-button-floating-hover": {
				light: "#F0F5FB",
				dark: "#1C2A3D"
			},
			"--dsw-alias-button-contrast-fill": {
				light: "#26364D",
				dark: "#EAF2FC"
			},
			"--dsw-alias-button-ghost-active-fill": {
				light: "#DCE7F4",
				dark: "#1C2A3D"
			},
			"--dsw-alias-button-ghost-active-hover": {
				light: "#E9F0F8",
				dark: "#162130"
			},
			"--dsw-alias-button-ghost-active-border": {
				light: "#8FA3BC",
				dark: "#6B829F"
			},
			"--dsw-alias-interactive-bg-hover": {
				light: "rgba(63, 118, 216, 0.08)",
				dark: "rgba(126, 164, 223, 0.1)"
			},
			"--dsw-alias-interactive-bg-hover-accent": {
				light: "rgba(63, 118, 216, 0.14)",
				dark: "rgba(126, 164, 223, 0.2)"
			},
			"--dsw-alias-interactive-bg-active": {
				light: "rgba(63, 118, 216, 0.2)",
				dark: "rgba(126, 164, 223, 0.26)"
			},
			"--dsw-alias-interactive-bg-hover-danger": {
				light: "rgba(236, 19, 19, 0.05)",
				dark: "rgba(242, 90, 90, 0.14)"
			},
			"--dsw-alias-interactive-bg-hover-solid": {
				light: "#F0F5FB",
				dark: "#1C2A3D"
			},
			"--dsw-alias-markdown-code-block": {
				light: "#F0F5FB",
				dark: "#0D141F"
			},
			"--dsw-alias-markdown-code-block-banner": {
				light: "#F5F8FD",
				dark: "#121B29"
			},
			"--dsw-alias-markdown-inline-code": {
				light: "#E4EDF8",
				dark: "#172334"
			},
			"--dsw-alias-markdown-citation": {
				light: "#EAF1F9",
				dark: "#1A2534"
			},
			"--dsw-alias-markdown-tag": {
				light: "#E4EDF8",
				dark: "#162130"
			},
			"--dsw-alias-markdown-placeholder": {
				light: "#EAF1F9",
				dark: "#131D2B"
			},
			"--dsw-alias-markdown-code-segment-selected": {
				light: "#FFFFFF",
				dark: "#1C2A3D"
			},
			"--dsw-alias-markdown-code-segment-unselected": {
				light: "#F0F5FB",
				dark: "#0F1723"
			},
			"--dsw-alias-scrollbar-bg-l1": {
				light: "rgba(63, 118, 216, 0.28)",
				dark: "rgba(126, 164, 223, 0.28)"
			},
			"--dsw-alias-scrollbar-bg-l2": {
				light: "rgba(63, 118, 216, 0.4)",
				dark: "rgba(126, 164, 223, 0.36)"
			},
			"--dsw-alias-scrollbar-hover-l1": {
				light: "rgba(63, 118, 216, 0.5)",
				dark: "rgba(126, 164, 223, 0.44)"
			},
			"--dsw-alias-scrollbar-hover-l2": {
				light: "rgba(63, 118, 216, 0.6)",
				dark: "rgba(126, 164, 223, 0.52)"
			},
			"--dsw-specific-sidebar-fill": {
				light: "transparent",
				dark: "transparent"
			},
			"--dsw-specific-sidebar-nav-item-active": {
				light: "#DEE9F8",
				dark: "#1B283A"
			},
			"--dsw-specific-sidebar-nav-item-hover": {
				light: "#E9F0F8",
				dark: "#15202F"
			},
			"--dsw-specific-sidebar-nav-item-active-accent": {
				light: "#3F76D8",
				dark: "#6E9BE8"
			},
			"--dsw-specific-input-major": {
				light: "#FFFFFF",
				dark: "#101927"
			},
			"--dsw-specific-login-input": {
				light: "#F0F5FB",
				dark: "#0D141F"
			},
			"--dsw-specific-menu": {
				light: "#EAF1F9",
				dark: "#162130"
			},
			"--dsw-specific-selector": {
				light: "#EAF1F9",
				dark: "#1C2A3D"
			},
			"--dsw-specific-bubble": {
				light: "#F0F5FC",
				dark: "#121C2A"
			},
			"--dsw-specific-bubble-highlight": {
				light: "#DCE9FB",
				dark: "#1A283A"
			},
			"--dsw-specific-tip": {
				light: "#EAF1F9",
				dark: "#131D2B"
			},
			"--dsw-alias-toast-bg": {
				light: "#1B3256",
				dark: "#1C2A3D"
			},
			"--dsw-alias-tooltip-bg": {
				light: "#13243E",
				dark: "#162130"
			},
			"--dsw-shadow-lv1": {
				light: "0 2px 4px rgba(19, 45, 83, 0.06)",
				dark: "0 2px 4px rgba(2, 6, 14, 0.5)"
			},
			"--dsw-shadow-lv1-blur": {
				light: "0 4px 12px rgba(19, 45, 83, 0.05)",
				dark: "0 4px 12px rgba(2, 6, 14, 0.4)"
			},
			"--dsw-shadow-lv2": {
				light: "0 4px 12px rgba(19, 45, 83, 0.05), 0 2px 8px rgba(19, 45, 83, 0.06)",
				dark: "0 4px 12px rgba(2, 6, 14, 0.4), 0 2px 8px rgba(2, 6, 14, 0.35)"
			},
			"--dsw-shadow-lv3": {
				light: "0 0 1px rgba(19, 45, 83, 0.08), 0 12px 32px rgba(19, 45, 83, 0.12)",
				dark: "0 0 1px rgba(2, 6, 14, 0.6), 0 12px 32px rgba(2, 6, 14, 0.55)"
			}
		};
		/**
		* Compatibility-mode token set: the same palette as the floating mode, but
		* every surface token turns translucent, so the fluid/wallpaper backdrop
		* shows through the STOCK layout. This is what makes the material generic —
		* any plugin that consumes the shared design tokens gets the glass for free.
		*/
		const COMPAT_SURFACE_OVERRIDES = {
			"--dsw-alias-bg-layer-1": {
				light: "rgba(255, 255, 255, 0.55)",
				dark: "rgba(17, 26, 39, 0.55)"
			},
			"--dsw-alias-bg-layer-2": {
				light: "rgba(236, 242, 250, 0.5)",
				dark: "rgba(22, 33, 48, 0.55)"
			},
			"--dsw-alias-bg-layer-3": {
				light: "rgba(226, 235, 247, 0.45)",
				dark: "rgba(28, 42, 61, 0.5)"
			},
			"--dsw-alias-bg-overlay": {
				light: "rgba(220, 231, 244, 0.6)",
				dark: "rgba(34, 51, 74, 0.6)"
			},
			"--dsw-alias-bg-module-platform": {
				light: "rgba(255, 255, 255, 0.55)",
				dark: "rgba(17, 26, 39, 0.55)"
			},
			"--dsw-alias-bg-multi-select": {
				light: "rgba(255, 255, 255, 0.55)",
				dark: "rgba(22, 33, 48, 0.55)"
			},
			"--dsw-specific-menu": {
				light: "rgba(234, 241, 249, 0.6)",
				dark: "rgba(22, 33, 48, 0.6)"
			},
			"--dsw-specific-selector": {
				light: "rgba(234, 241, 249, 0.55)",
				dark: "rgba(28, 42, 61, 0.55)"
			},
			"--dsw-specific-bubble": {
				light: "rgba(240, 245, 252, 0.55)",
				dark: "rgba(18, 28, 42, 0.55)"
			},
			"--dsw-specific-bubble-highlight": {
				light: "rgba(220, 233, 251, 0.55)",
				dark: "rgba(26, 40, 58, 0.55)"
			},
			"--dsw-specific-tip": {
				light: "rgba(234, 241, 249, 0.6)",
				dark: "rgba(19, 29, 43, 0.6)"
			},
			"--dsw-specific-input-major": {
				light: "rgba(255, 255, 255, 0.5)",
				dark: "rgba(16, 25, 39, 0.5)"
			},
			"--dsw-specific-login-input": {
				light: "rgba(240, 245, 251, 0.5)",
				dark: "rgba(13, 20, 31, 0.5)"
			},
			"--dsw-alias-markdown-code-block": {
				light: "rgba(240, 245, 251, 0.5)",
				dark: "rgba(13, 20, 31, 0.5)"
			},
			"--dsw-alias-markdown-code-block-banner": {
				light: "rgba(245, 248, 253, 0.55)",
				dark: "rgba(18, 27, 41, 0.55)"
			},
			"--dsw-alias-markdown-inline-code": {
				light: "rgba(228, 237, 248, 0.5)",
				dark: "rgba(23, 35, 52, 0.5)"
			},
			"--dsw-alias-markdown-citation": {
				light: "rgba(234, 241, 249, 0.55)",
				dark: "rgba(26, 37, 52, 0.55)"
			},
			"--dsw-alias-markdown-tag": {
				light: "rgba(228, 237, 248, 0.5)",
				dark: "rgba(22, 33, 48, 0.5)"
			},
			"--dsw-alias-markdown-placeholder": {
				light: "rgba(234, 241, 249, 0.55)",
				dark: "rgba(19, 29, 43, 0.55)"
			},
			"--dsw-alias-toast-bg": {
				light: "rgba(27, 50, 86, 0.85)",
				dark: "rgba(28, 42, 61, 0.85)"
			},
			"--dsw-alias-tooltip-bg": {
				light: "rgba(19, 36, 62, 0.88)",
				dark: "rgba(22, 33, 48, 0.88)"
			}
		};
		/** Compatibility token layer: the palette plus the translucent surfaces. */
		const COMPAT_TOKEN_OVERRIDES = {
			...AQUA_TOKEN_OVERRIDES,
			...COMPAT_SURFACE_OVERRIDES,
			...glassSurfaceTokens()
		};
		/** Shared materials also cover the stock desktop's settings and plugin cards.
		* Keep the opaque base as the backdrop, then clear only structural shell seams. */
		function glassSurfaceTokens() {
			return {
				"--dsw-alias-bg-layer-1": both("var(--dsh-aqua-material-read)"),
				"--dsw-alias-bg-layer-2": both("var(--dsh-aqua-material-pane)"),
				"--dsw-alias-bg-layer-3": both("var(--dsh-aqua-material-strong)"),
				"--dsw-alias-bg-overlay": both("var(--dsh-aqua-material-overlay)"),
				"--dsw-alias-bg-module-platform": both("var(--dsh-aqua-material-control)"),
				"--dsw-alias-bg-multi-select": both("var(--dsh-aqua-material-control)"),
				"--dsw-alias-button-elevated-fill": both("var(--dsh-aqua-material-control)"),
				"--dsw-alias-button-floating-fill": both("var(--dsh-aqua-material-control)"),
				"--dsw-alias-button-floating-hover": both("var(--dsh-aqua-material-selected)"),
				"--dsw-alias-settings-card-fill": both("var(--dsh-aqua-material-read)"),
				"--dsw-alias-settings-card-stroke": both("var(--dsw-alias-border-l2)"),
				"--dsw-specific-input-major": both("var(--dsh-aqua-material-input)"),
				"--dsw-specific-login-input": both("var(--dsh-aqua-material-input)"),
				"--dsw-specific-menu": both("var(--dsh-aqua-material-overlay)"),
				"--dsw-specific-selector": both("var(--dsh-aqua-material-control)"),
				"--dsw-specific-sidebar-nav-item-active": both("var(--dsh-aqua-material-selected)"),
				"--dsw-specific-sidebar-nav-item-hover": both("var(--dsh-aqua-material-control)"),
				"--dsw-menu-backdrop-filter": both("blur(var(--dsh-aqua-blur, 14px)) saturate(125%)")
			};
		}
		const FLOAT_TOKEN_OVERRIDES = {
			...AQUA_TOKEN_OVERRIDES,
			...glassSurfaceTokens()
		};
		/** Read the persisted enable flag (absent storage means off). */
		function readEnabled() {
			try {
				const raw = localStorage.getItem(AQUA_ENABLED_KEY);
				return raw === "true";
			} catch {
				return false;
			}
		}
		/** Persist the enable flag (storage failures keep the in-memory state). */
		function writeEnabled(value) {
			try {
				localStorage.setItem(AQUA_ENABLED_KEY, String(value));
			} catch {}
		}
		/** Shipped defaults — what a first-time install sees (the tuned look). */
		const SETTINGS_DEFAULTS = {
			mode: "mica",
			blur: 20,
			// ORION default: keep cards readable over the fluid canvas. The upstream
			// value (7) made large text and stage data look washed out.
			frost: 45,
			ontologySurfaceStrength: 60,
			bgBrightness: 50,
			background: "fluid",
			wallpaper: "",
			mesh: false,
			spotlight: true,
			press: true,
			fluidHue: 320,
			fluidDepth: 14,
			wallpaperBlur: 0,
			wallpaperFrost: 0,
			videoBlur: 6,
			videoBrightness: 45
		};
		/** Numeric knob keys and their localStorage names. */
		const NUMERIC_KEYS = {
			blur: "dsh.ui-aqua.blur",
			frost: "dsh.ui-aqua.frost",
			ontologySurfaceStrength: "dsh.ui-aqua.ontologySurfaceStrength",
			fluidHue: "dsh.ui-aqua.fluidHue",
			fluidDepth: "dsh.ui-aqua.fluidDepth",
			bgBrightness: "dsh.ui-aqua.bgBrightness",
			wallpaperBlur: "dsh.ui-aqua.wallpaperBlur",
			wallpaperFrost: "dsh.ui-aqua.wallpaperFrost",
			videoBlur: "dsh.ui-aqua.videoBlur",
			videoBrightness: "dsh.ui-aqua.videoBrightness"
		};
		const MODE_KEY = "dsh.ui-aqua.mode";
		const BACKGROUND_KEY = "dsh.ui-aqua.background";
		const WALLPAPER_KEY = "dsh.ui-aqua.wallpaper";
		const MESH_KEY = "dsh.ui-aqua.mesh";
		const SPOTLIGHT_KEY = "dsh.ui-aqua.spotlight";
		const PRESS_KEY = "dsh.ui-aqua.press";
		/** Clamp a numeric knob into its sane range. */
		function clampSetting(key, value) {
			const max = key === "blur" || key === "wallpaperBlur" || key === "videoBlur" ? 40 : key === "frost" || key === "ontologySurfaceStrength" || key === "wallpaperFrost" || key === "bgBrightness" || key === "videoBrightness" ? 100 : 360;
			return Number.isFinite(value) ? Math.min(max, Math.max(0, value)) : SETTINGS_DEFAULTS[key];
		}
		/** Read one numeric knob from localStorage (absent/parse failure means the default). */
		function readSetting(key) {
			try {
				const raw = localStorage.getItem(NUMERIC_KEYS[key]);
				return raw === null ? SETTINGS_DEFAULTS[key] : clampSetting(key, Number(raw));
			} catch {
				return SETTINGS_DEFAULTS[key];
			}
		}
		/** Persist one numeric knob (storage failures keep the in-memory state). */
		function writeSetting(key, value) {
			try {
				localStorage.setItem(NUMERIC_KEYS[key], String(value));
			} catch {}
		}
		/** Read the backdrop source ('fluid' or 'wallpaper'). */
		function readBackground() {
			try {
				return localStorage.getItem(BACKGROUND_KEY) === "wallpaper" ? "wallpaper" : "fluid";
			} catch {
				return "fluid";
			}
		}
		/** Persist the backdrop source. */
		function writeBackground(value) {
			try {
				localStorage.setItem(BACKGROUND_KEY, value);
			} catch {
				throw new Error("WALLPAPER_STORAGE_FAILED");
			}
		}
		/** Read the rendering mode ('mica' or 'compat'; legacy 'float'/'liquid'
		*  values migrate to 'mica'). */
		function readMode() {
			try {
				if (localStorage.getItem(MODE_KEY) === "compat") return "compat";
				return "mica";
			} catch {
				return "mica";
			}
		}
		/** Persist the rendering mode. */
		function writeMode(value) {
			try {
				localStorage.setItem(MODE_KEY, value);
			} catch {}
		}
		/** Read the wallpaper data URL (absent/oversized means empty). */
		function readWallpaper() {
			try {
				return localStorage.getItem(WALLPAPER_KEY) ?? "";
			} catch {
				return "";
			}
		}
		/** Save before applying: a failed preference write must not look durable. */
		function writeWallpaper(value) {
			try {
				localStorage.setItem(WALLPAPER_KEY, value);
			} catch {
				throw new Error("WALLPAPER_STORAGE_FAILED");
			}
		}
		/** Read the interactive-mesh flag (absent means off for the production workbench). */
		function readMesh() {
			try {
				const raw = localStorage.getItem(MESH_KEY);
				return raw === null ? false : raw === "true";
			} catch {
				return false;
			}
		}
		/** Persist the interactive-mesh flag. */
		function writeMesh(value) {
			try {
				localStorage.setItem(MESH_KEY, String(value));
			} catch {}
		}
		/** Read the cursor-spotlight flag (absent means on). */
		function readSpotlight() {
			try {
				const raw = localStorage.getItem(SPOTLIGHT_KEY);
				return raw === null ? true : raw === "true";
			} catch {
				return true;
			}
		}
		/** Persist the cursor-spotlight flag. */
		function writeSpotlight(value) {
			try {
				localStorage.setItem(SPOTLIGHT_KEY, String(value));
			} catch {}
		}
		/** Read the hover-press flag (absent means on). */
		function readPress() {
			try {
				localStorage.removeItem("dsh.ui-aqua.entrance");
				const raw = localStorage.getItem(PRESS_KEY);
				return raw === null ? true : raw === "true";
			} catch {
				return true;
			}
		}
		/** Persist the hover-press flag. */
		function writePress(value) {
			try {
				localStorage.setItem(PRESS_KEY, String(value));
			} catch {}
		}
		/** Current scheme from the presenter-owned body attribute. */
		function activeScheme() {
			return document.body.hasAttribute("data-ds-dark-theme") ? "dark" : "light";
		}
		/**
		* Owns the Aqua layer lifecycle: reads the durable enable flag, and applies /
		* retracts every layer on change. Cross-tab flips arrive through the storage
		* event; every subscription and mounted effect are released when the plugin
		* fiber is disposed.
		*/
		var AquaLayer = class {
			enabled = false;
			settings = { ...SETTINGS_DEFAULTS };
			/** Resolved palette scheme: dark = the brightness knob darkens, light = it brightens. */
			dark = false;
			tokenDisposer;
			mainFluid;
			interactionDisposer;
			themeListener;
			seamDisposer;
			spotlightDisposer;
			meshHandle;
			/** Object URL of the current large-video wallpaper (revoked on replace). */
			videoObjectUrl;
			/** IndexedDB id backing the current object URL (guards against reloads). */
			videoBlobId;
			videoLoadingId;
			mediaRevision = 0;
			mediaError = "";
			onMediaChange;
			ctx;
			/**
			* @param ctx - owning client context.
			*/
			constructor(ctx) {
				this.ctx = ctx;
				ctx.effect(() => {
					const onStorage = (event) => {
						if (event.key === "dsh.ui-aqua.enabled") {
							this.enabled = readEnabled();
							this.sync();
						}
						const key = event.key;
						if (key !== null && (Object.values(NUMERIC_KEYS).includes(key) || key === BACKGROUND_KEY || key === WALLPAPER_KEY || key === MODE_KEY || key === MESH_KEY || key === SPOTLIGHT_KEY || key === PRESS_KEY)) {
							if (key === BACKGROUND_KEY || key === WALLPAPER_KEY) {
								this.releaseWallpaperVideo(document.querySelector("[data-dsh-aqua-wallpaper-video]"));
								this.mediaError = "";
								}
							this.reloadSettings();
							if (this.enabled) {
								this.applySettings();
								this.applyTokens();
								this.applyFluidPalettes();
							}
						}
					};
					window.addEventListener("storage", onStorage);
					const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
					const updatePlayback = () => {
						const video = document.querySelector("[data-dsh-aqua-wallpaper-video]");
						if (this.enabled && video && !video.hidden && video.getAttribute("src")) this.configureWallpaperVideo(video);
					};
					const updateMotion = () => {
						updatePlayback();
						this.teardownFluid();
						if (this.enabled) this.mountFluid();
					};
					document.addEventListener("visibilitychange", updatePlayback);
					reducedMotion.addEventListener("change", updateMotion);
					this.themeListener = this.ctx.on("theme/change", () => {
						this.dark = this.resolveScheme();
					if (this.enabled) {
							this.applySettings();
							this.applyFluidPalettes();
						}
					});
					return () => {
						window.removeEventListener("storage", onStorage);
						document.removeEventListener("visibilitychange", updatePlayback);
						reducedMotion.removeEventListener("change", updateMotion);
						this.themeListener?.();
						this.themeListener = void 0;
						this.unmount();
					};
				}, "ui-aqua: layer lifecycle");
				this.enabled = readEnabled();
				this.reloadSettings();
				this.dark = this.resolveScheme();
				this.sync();
			}
			/** Current enable state (the settings row mirrors this). */
			getEnabled() {
				return this.enabled;
			}
			/** Current knob values (the settings row mirrors these). */
			getSettings() {
				return { ...this.settings };
			}
			/** Whether the resolved palette is dark (the brightness knob darkens). */
			getDark() {
				return this.dark;
			}
			/** Resolved scheme from the theme service (falls back to the body attribute). */
			resolveScheme() {
				try {
					return this.ctx.theme.getTheme().active.colorScheme === "dark";
				} catch {
					return activeScheme() === "dark";
				}
			}
			/** Re-read every knob from localStorage into memory. */
			reloadSettings() {
				try {
					localStorage.removeItem("dsh.ui-aqua.tilt");
					localStorage.removeItem("dsh.ui-aqua.lens");
					localStorage.removeItem("dsh.ui-aqua.fluidTone");
				} catch {}
				this.settings = {
					mode: readMode(),
					blur: readSetting("blur"),
					frost: readSetting("frost"),
					ontologySurfaceStrength: readSetting("ontologySurfaceStrength"),
					fluidHue: readSetting("fluidHue"),
					fluidDepth: readSetting("fluidDepth"),
					bgBrightness: readSetting("bgBrightness"),
					background: readBackground(),
					wallpaper: readWallpaper(),
					mesh: readMesh(),
					spotlight: readSpotlight(),
					press: readPress(),
					wallpaperBlur: readSetting("wallpaperBlur"),
					wallpaperFrost: readSetting("wallpaperFrost"),
					videoBlur: readSetting("videoBlur"),
					videoBrightness: readSetting("videoBrightness")
				};
			}
			/** Flip the layer: persist, then apply or retract every owned effect. */
			setEnabled(value) {
				if (value === this.enabled) return;
				this.enabled = value;
				writeEnabled(value);
				this.sync();
			}
			/** Set the rendering mode ('mica' or 'compat'). */
			setMode(value) {
				if (value === this.settings.mode) return;
				this.settings.mode = value;
				writeMode(value);
				if (this.enabled) {
					this.applySettings();
					this.applyTokens();
				}
			}
			/** Set the glass blur radius (px). */
			setBlur(value) {
				const next = clampSetting("blur", value);
				if (next === this.settings.blur) return;
				this.settings.blur = next;
				writeSetting("blur", next);
				if (this.enabled) this.applySettings();
			}
			/** Set the glass frost amount (0-100). */
			setFrost(value) {
				const next = clampSetting("frost", value);
				if (next === this.settings.frost) return;
				this.settings.frost = next;
				writeSetting("frost", next);
				if (this.enabled) this.applySettings();
			}
			/** Set the Ontology Center surface strength (0-100). */
			setOntologySurfaceStrength(value) {
				const next = clampSetting("ontologySurfaceStrength", value);
				if (next === this.settings.ontologySurfaceStrength) return;
				this.settings.ontologySurfaceStrength = next;
				writeSetting("ontologySurfaceStrength", next);
				if (this.enabled) this.applySettings();
			}
			/** Set the fluid hue (degrees, continuous). */
			setFluidHue(value) {
				const next = clampSetting("fluidHue", value);
				if (next === this.settings.fluidHue) return;
				this.settings.fluidHue = next;
				writeSetting("fluidHue", next);
				if (this.enabled) {
					this.applySettings();
					this.applyFluidPalettes();
				}
			}
			/** Set the fluid depth (0-100, continuous: deep ↔ pale). */
			setFluidDepth(value) {
				const next = clampSetting("fluidDepth", value);
				if (next === this.settings.fluidDepth) return;
				this.settings.fluidDepth = next;
				writeSetting("fluidDepth", next);
				if (this.enabled) this.applyFluidPalettes();
			}
			/** Set the background brightness (0-100: 0 = pure black, 50 = transparent, 100 = pure white). */
			setBgBrightness(value) {
				const next = clampSetting("bgBrightness", value);
				if (next === this.settings.bgBrightness) return;
				this.settings.bgBrightness = next;
				writeSetting("bgBrightness", next);
				if (this.enabled) this.applySettings();
			}
			/** Set the backdrop source (fluid board or custom wallpaper). */
			setBackground(value) {
				if (value === this.settings.background) return;
				writeBackground(value);
				this.releaseWallpaperVideo(document.querySelector("[data-dsh-aqua-wallpaper-video]"));
				this.mediaError = "";
				this.settings.background = value;
				if (this.enabled) this.applySettings();
			}
			/** Set the wallpaper image (a data URL; empty clears it) or a large video
			*  (`idb:<id>` marker whose blob lives in IndexedDB). */
			setWallpaper(value) {
				const previous = this.settings.wallpaper;
				writeWallpaper(value);
				this.releaseWallpaperVideo(document.querySelector("[data-dsh-aqua-wallpaper-video]"));
				this.settings.wallpaper = value;
				this.mediaError = "";
				if (previous.startsWith("idb:") && value !== previous) deleteVideoBlob(previous.slice(4));
				if (this.enabled) this.applySettings();
			}
			/** Set the interactive-mesh flag (dot-grid decoration). */
			setMesh(value) {
				if (value === this.settings.mesh) return;
				this.settings.mesh = value;
				writeMesh(value);
				if (this.enabled) this.syncMesh();
			}
			/** Set the cursor-spotlight flag (pointer-tracking glass glow). */
			setSpotlight(value) {
				if (value === this.settings.spotlight) return;
				this.settings.spotlight = value;
				writeSpotlight(value);
				if (this.enabled) this.applySettings();
			}
			/** Set the hover-press flag (pane sinks a touch under the cursor). */
			setPress(value) {
				if (value === this.settings.press) return;
				this.settings.press = value;
				writePress(value);
				if (this.enabled) this.applySettings();
			}
			/** Set the wallpaper blur radius (px). */
			setWallpaperBlur(value) {
				const next = clampSetting("wallpaperBlur", value);
				if (next === this.settings.wallpaperBlur) return;
				this.settings.wallpaperBlur = next;
				writeSetting("wallpaperBlur", next);
				if (this.enabled) this.applySettings();
			}
			/** Set the wallpaper frost veil (0-100). */
			setWallpaperFrost(value) {
				const next = clampSetting("wallpaperFrost", value);
				if (next === this.settings.wallpaperFrost) return;
				this.settings.wallpaperFrost = next;
				writeSetting("wallpaperFrost", next);
				if (this.enabled) this.applySettings();
			}
			/** Set the video wallpaper blur radius (px). */
			setVideoBlur(value) {
				const next = clampSetting("videoBlur", value);
				if (next === this.settings.videoBlur) return;
				this.settings.videoBlur = next;
				writeSetting("videoBlur", next);
				if (this.enabled) this.applySettings();
			}
			/** Set the video wallpaper brightness (0-100, 100 = fully lit). */
			setVideoBrightness(value) {
				const next = clampSetting("videoBrightness", value);
				if (next === this.settings.videoBrightness) return;
				this.settings.videoBrightness = next;
				writeSetting("videoBrightness", next);
				if (this.enabled) this.applySettings();
			}
			/** After the user re-grants file access (选择视频 click on an fsa: video),
			*  drop the mount guard and re-apply so the file is re-read and played. */
			authorizeVideo() {
				this.releaseWallpaperVideo(document.querySelector("[data-dsh-aqua-wallpaper-video]"));
				this.mediaError = "";
				if (this.enabled) this.applySettings();
			}
			/** Invalidate pending reads before releasing a decoder or object URL. */
			releaseWallpaperVideo(video) {
				this.mediaRevision += 1;
				this.videoLoadingId = void 0;
				if (video) {
					video.onerror = null;
					video.onloadeddata = null;
					video.autoplay = false;
					video.pause();
					if (video.getAttribute("src") !== null) {
						video.removeAttribute("src");
						video.load();
					}
				}
				if (this.videoObjectUrl !== void 0) URL.revokeObjectURL(this.videoObjectUrl);
				this.videoObjectUrl = void 0;
				this.videoBlobId = void 0;
			}
			isCurrentWallpaper(wallpaper, video, revision) {
				return this.enabled && this.settings.background === "wallpaper" && this.settings.wallpaper === wallpaper && this.mediaRevision === revision && document.querySelector("[data-dsh-aqua-wallpaper-video]") === video;
			}
			failWallpaper(code) {
				this.mediaError = code;
				this.releaseWallpaperVideo(document.querySelector("[data-dsh-aqua-wallpaper-video]"));
				if (this.enabled) this.applySettings();
				this.onMediaChange?.();
			}
			sync() {
				if (this.enabled) this.mount();
				else this.unmount();
			}
			/** Write the knob-driven CSS variables and mode attributes onto <html>. */
			applySettings() {
				const style = document.documentElement.style;
				style.setProperty("--dsh-aqua-blur", `${this.settings.blur}px`);
				style.setProperty("--dsh-aqua-frost", String(Math.min(this.settings.frost / 50, 1.4)));
				style.setProperty("--dsh-aqua-surface-frost", String(Math.min((this.settings.frost + 20) / 50, 1.4)));
				style.setProperty("--dsh-aqua-ontology-surface-strength", String(this.settings.ontologySurfaceStrength / 100));
				const glowHue = ((this.settings.fluidHue + 217) % 360 + 360) % 360;
				style.setProperty("--dsh-aqua-spot-color", this.dark ? `hsla(${glowHue}, 90%, 62%, 0.17)` : `hsla(${glowHue}, 90%, 45%, 0.16)`);
				style.setProperty("--dsh-aqua-wallpaper-blur", `${this.settings.wallpaperBlur}px`);
				style.setProperty("--dsh-aqua-wallpaper-frost", String(this.settings.wallpaperFrost / 100));
				style.setProperty("--dsh-aqua-video-blur", `${this.settings.videoBlur}px`);
				style.setProperty("--dsh-aqua-video-dim", String((100 - this.settings.videoBrightness) / 100 * .65));
				const dark = this.dark;
				style.setProperty("--dsh-aqua-brightness-black", String(dark ? Math.max(0, (50 - this.settings.bgBrightness) / 50) : 0));
				style.setProperty("--dsh-aqua-brightness-white", String(dark ? 0 : Math.max(0, (this.settings.bgBrightness - 50) / 50)));
				const compat = this.settings.mode === "compat";
				document.documentElement.toggleAttribute("data-dsh-float", !compat);
				document.documentElement.toggleAttribute("data-dsh-compat", compat);
				document.documentElement.toggleAttribute(SPOTLIGHT_ATTRIBUTE, !compat && this.settings.spotlight);
				document.documentElement.toggleAttribute(PRESS_ATTRIBUTE, !compat && this.settings.press);
				const wallpaper = this.settings.wallpaper;
				const isVideo = wallpaper.startsWith("data:video/") || wallpaper.startsWith("idb:") || wallpaper.startsWith("fsa:");
				const wallpaperOn = this.settings.background === "wallpaper" && wallpaper !== "";
				const wallpaperVisible = wallpaperOn && this.mediaError === "";
				const visualBackground = wallpaperVisible ? "wallpaper" : "fluid";
				const ambient = document.querySelector("[data-dsh-aqua-ambient]");
				if (ambient !== null) ambient.dataset.background = visualBackground;
				const wallpaperLayer = document.querySelector("[data-dsh-aqua-wallpaper-layer]");
				if (wallpaperLayer !== null) {
					wallpaperLayer.dataset.background = visualBackground;
					wallpaperLayer.dataset.media = isVideo ? "video" : "image";
					wallpaperLayer.hidden = !wallpaperVisible;
				}
				document.documentElement.toggleAttribute("data-dsh-aqua-wallpaper", wallpaperVisible);
				if (wallpaperVisible) document.documentElement.setAttribute("data-dsh-aqua-media", isVideo ? "video" : "image");
				else document.documentElement.removeAttribute("data-dsh-aqua-media");
				const img = document.querySelector("[data-dsh-aqua-wallpaper-img]");
				if (img !== null) {
					img.hidden = !wallpaperVisible || isVideo;
					if (!img.hidden) {
						img.onerror = () => {
							if (this.enabled && this.settings.wallpaper === wallpaper && this.settings.background === "wallpaper") this.failWallpaper("aqua.mediaUnavailable");
						};
						if (img.getAttribute("src") !== wallpaper) img.src = wallpaper;
					} else {
						img.onerror = null;
						img.removeAttribute("src");
					}
				}
				const video = document.querySelector("[data-dsh-aqua-wallpaper-video]");
				if (video !== null) {
					video.hidden = !wallpaperVisible || !isVideo;
					if (!video.hidden) {
						const revision = this.mediaRevision;
						video.onerror = () => {
							if (this.isCurrentWallpaper(wallpaper, video, revision)) this.failWallpaper("aqua.videoPlaybackFailed");
						};
						if (wallpaper.startsWith("idb:") || wallpaper.startsWith("fsa:")) {
							if (this.videoBlobId === wallpaper && this.videoObjectUrl !== void 0) {
								if (video.getAttribute("src") !== this.videoObjectUrl) video.setAttribute("src", this.videoObjectUrl);
								this.configureWallpaperVideo(video);
							} else if (this.videoLoadingId !== wallpaper) {
								this.videoLoadingId = wallpaper;
								const read = async () => {
									if (wallpaper.startsWith("idb:")) return loadVideoBlob(wallpaper.slice(4));
									const handle = await loadVideoHandle();
									if (handle === null || await handle.queryPermission({ mode: "read" }) !== "granted") throw new Error("VIDEO_PERMISSION_REQUIRED");
									return handle.getFile();
								};
								read().then((blob) => {
									if (!this.isCurrentWallpaper(wallpaper, video, revision)) return;
									if (blob === null) throw new Error("MEDIA_UNAVAILABLE");
									const url = URL.createObjectURL(blob);
									if (this.videoObjectUrl !== void 0) URL.revokeObjectURL(this.videoObjectUrl);
									this.videoObjectUrl = url;
									this.videoBlobId = wallpaper;
									video.setAttribute("src", url);
									this.configureWallpaperVideo(video);
								}).catch((error) => {
									if (this.isCurrentWallpaper(wallpaper, video, revision)) this.failWallpaper(error.message === "VIDEO_PERMISSION_REQUIRED" ? "aqua.videoPermissionRequired" : "aqua.mediaUnavailable");
								}).finally(() => {
									if (this.mediaRevision === revision) this.videoLoadingId = void 0;
								});
							}
						} else {
							if (video.getAttribute("src") !== wallpaper) video.setAttribute("src", wallpaper);
							this.configureWallpaperVideo(video);
						}
					} else this.releaseWallpaperVideo(video);
				}
				if (wallpaperVisible) this.teardownFluid();
				else if (this.enabled) this.mountFluid();
			}
			/** The wallpaper plays as a plain <video> element (the browser's own
			*  decoder, no player chrome at all): looping on, cover fill via CSS, and
			*  autoplay with a muted fallback where policy requires it. A direct
			*  element (not an iframe) keeps backdrop-filter working over it, so the
			*  glass panels stay frosted above the video. */
			configureWallpaperVideo(video) {
				const wallpaper = this.settings.wallpaper, revision = this.mediaRevision;
				const shouldPlay = !window.matchMedia("(prefers-reduced-motion: reduce)").matches && !document.hidden;
				video.loop = true;
				video.muted = true;
				video.defaultMuted = true;
				video.autoplay = shouldPlay;
				video.playsInline = true;
				if (!shouldPlay) { video.pause(); return; }
				if (!video.paused) return;
				video.play().catch(() => {
					if (!document.hidden && !window.matchMedia("(prefers-reduced-motion: reduce)").matches && this.isCurrentWallpaper(wallpaper, video, revision)) this.failWallpaper("aqua.videoPlaybackFailed");
				});
			}
			/** Apply the mode's token layer (floating palette, or translucent compat). */
			applyTokens() {
				this.tokenDisposer?.();
				this.tokenDisposer = this.ctx.theme.overrideTokens(OVERRIDE_SOURCE, this.settings.mode === "compat" ? COMPAT_TOKEN_OVERRIDES : FLOAT_TOKEN_OVERRIDES);
			}
			mount() {
				document.documentElement.setAttribute(AQUA_ATTRIBUTE, "");
				ensureAmbientScene();
				ensurePageFades();
				this.applySettings();
				this.applyTokens();
				this.mountFluid();
				this.startSeamStamper();
				this.startSpotlightFeed();
				this.syncMesh();
			}
			/** Mount or drop the interactive mesh to match enabled + the mesh flag. */
			syncMesh() {
				if (this.enabled && this.settings.mesh) {
					if (this.meshHandle !== void 0) return;
					const ambient = document.querySelector("[data-dsh-aqua-ambient]");
					if (ambient === null) return;
					this.meshHandle = mountMesh(ambient);
				} else {
					this.meshHandle?.dispose();
					this.meshHandle = void 0;
				}
			}
			unmount() {
				this.releaseWallpaperVideo(document.querySelector("[data-dsh-aqua-wallpaper-video]"));
				document.documentElement.removeAttribute(AQUA_ATTRIBUTE);
				document.documentElement.removeAttribute("data-dsh-float");
				document.documentElement.removeAttribute("data-dsh-compat");
				document.documentElement.removeAttribute("data-dsh-aqua-wallpaper");
				document.documentElement.removeAttribute("data-dsh-aqua-media");
				document.documentElement.removeAttribute(SPOTLIGHT_ATTRIBUTE);
				document.documentElement.removeAttribute(PRESS_ATTRIBUTE);
				this.spotlightDisposer?.();
				this.spotlightDisposer = void 0;
				this.meshHandle?.dispose();
				this.meshHandle = void 0;
				this.tokenDisposer?.();
				this.tokenDisposer = void 0;
				this.teardownFluid();
				removeAmbientScene();
				removePageFades();
				this.seamDisposer?.();
				this.seamDisposer = void 0;
			}
			/** Attach the fluid shader and the interaction feeds. */
			mountFluid() {
				if (this.mainFluid !== void 0 || !this.enabled || this.settings.background === "wallpaper" && this.settings.wallpaper !== "" && this.mediaError === "") return;
				const mainCanvas = document.querySelector("[data-dsh-aqua-fluid-canvas]");
				try {
					if (mainCanvas !== null) this.mainFluid = attachFluidShader(mainCanvas, this.fluidParams());
					this.applyFluidPalettes();
					if (this.mainFluid !== void 0 && mainCanvas !== null) this.interactionDisposer = attachFluidInteractions({
						main: this.mainFluid,
						mainCanvas
					});
				} catch {
					this.mainFluid = void 0;
				}
			}
			teardownFluid() {
				this.interactionDisposer?.();
				this.interactionDisposer = void 0;
				this.mainFluid?.dispose();
				this.mainFluid = void 0;
			}
			fluidParams() {
				return {
					...SITE_FLUID_PARAMS,
					...fluidToneColors(this.dark, this.settings.fluidHue, this.settings.fluidDepth)
				};
			}
			applyFluidPalettes() {
				this.mainFluid?.setParams(this.fluidParams());
			}
			/** Stamp the data-* seams the stylesheet keys off (self-contained mode). */
			startSeamStamper() {
				if (this.seamDisposer !== void 0) return;
				this.seamDisposer = startSeamStamper();
			}
			/** Attach the cursor-spotlight pointer feeds (idempotent per mount). */
			startSpotlightFeed() {
				if (this.spotlightDisposer !== void 0) return;
				this.spotlightDisposer = startSpotlight();
			}
		};
		//#endregion
		//#region \0dsh-css:D:\Hermes Work\deepseek-harness\packages\client\ui-aqua\src\client\aqua.module.css.mjs
		const css$1 = "[data-dsh-aqua] body{background:var(--dsw-alias-bg-base)}[data-dsh-aqua]{--dsh-aqua-glass-card-light:color-mix(in srgb, #fff calc(42% * var(--dsh-aqua-frost,1)), transparent);--dsh-aqua-glass-card-dark:color-mix(in srgb, #22262f calc(50% * var(--dsh-aqua-frost,1)), transparent)}[data-dsh-aqua] [data-dsh-frame],[data-dsh-aqua] [data-phase],[data-dsh-aqua] [data-dsh-details]{background:0 0}[data-dsh-float] [data-phase=active] > header{z-index:8;position:relative}[data-dsh-float] [class*=banner]{position:static}[data-dsh-float] [data-phase=active] [data-conversation-scroll]{margin-top:-95px;padding-top:107px}[data-dsh-aqua] [data-phase] [class*=composerSeat][class*=composerSeat]{background:0 0}[data-dsh-aqua] [data-dsh-aqua-ambient]{z-index:-1;pointer-events:none;background:radial-gradient(760px 420px at 50% -8%,#a0c8ff42,#0000 70%),linear-gradient(#9cc1e738 0%,#9cc1e700 38%),radial-gradient(900px 420px at 50% 108%,#9cc1e724,#0000 70%);position:fixed;inset:0;overflow:hidden}[data-dsh-aqua] body[data-ds-dark-theme] [data-dsh-aqua-ambient]{background:radial-gradient(760px 420px at 50% -8%,#6ea5ff21,#0000 70%),linear-gradient(#5e8fe021 0%,#5e8fe000 46%),radial-gradient(900px 420px at 50% 108%,#5e8fe017,#0000 70%)}[data-dsh-aqua] [data-dsh-aqua-ambient]:after{content:\"\";background-image:linear-gradient(rgba(255, 255, 255, var(--dsh-aqua-brightness-white,0)), rgba(255, 255, 255, var(--dsh-aqua-brightness-white,0))), linear-gradient(rgba(0, 0, 0, var(--dsh-aqua-brightness-black,0)), rgba(0, 0, 0, var(--dsh-aqua-brightness-black,0)));position:absolute;inset:0}@media (prefers-reduced-motion:no-preference){[data-dsh-aqua] [data-dsh-aqua-ambient]{animation:qRUgUq_dsh-aqua-breathe 9s var(--ds-ease-in-out) infinite alternate}}@keyframes qRUgUq_dsh-aqua-breathe{0%{opacity:.86}to{opacity:1}}[data-dsh-aqua] [data-dsh-aqua-fluid-canvas]{width:100%;height:100%;position:absolute;inset:0}[data-dsh-aqua] [data-dsh-aqua-wallpaper]{z-index:-1;position:fixed;inset:0;overflow:hidden}[data-dsh-aqua] [data-dsh-aqua-wallpaper-img]{object-fit:cover;width:100%;height:100%}[data-dsh-aqua] [data-dsh-aqua-wallpaper-video]{object-fit:cover;pointer-events:none;width:100%;height:100%;filter:blur(var(--dsh-aqua-video-blur,0px));border:0;position:absolute;inset:0}[data-dsh-aqua] [data-dsh-aqua-wallpaper-img]{filter:blur(var(--dsh-aqua-wallpaper-blur,0px))}[data-dsh-aqua] [data-dsh-aqua-wallpaper]:after{content:\"\";background:rgb(255 255 255/var(--dsh-aqua-wallpaper-frost,0));pointer-events:none;position:absolute;inset:0}[data-dsh-aqua] body[data-ds-dark-theme] [data-dsh-aqua-wallpaper]:after{background:rgb(12 18 27/var(--dsh-aqua-wallpaper-frost,0))}[data-dsh-aqua] [data-dsh-aqua-wallpaper][data-media=video]:after{background:rgb(255 255 255/calc(var(--dsh-aqua-video-dim,.36) * 1.3));display:block}[data-dsh-aqua] body[data-ds-dark-theme] [data-dsh-aqua-wallpaper][data-media=video]:after{background:rgb(8 12 20/var(--dsh-aqua-video-dim,.36))}[data-dsh-aqua] [data-dsh-aqua-ambient][data-background=wallpaper] [data-dsh-aqua-fluid-canvas],[data-dsh-aqua] [data-dsh-aqua-wallpaper][data-background=fluid]{display:none}[data-dsh-aqua] [data-dsh-aqua-mesh]{pointer-events:none;width:100%;height:100%;position:absolute;inset:0}[data-dsh-float] [role=menu],[data-dsh-float] [role=dialog],[data-dsh-float] [role=alert],[data-dsh-float] [data-dsh-surface]{border-radius:14px}[data-dsh-float] [data-dsh-surface]{background:color-mix(in srgb, #fff calc(62% * var(--dsh-aqua-surface-frost,1)), transparent);backdrop-filter:blur(var(--dsh-aqua-blur,14px));border:1px solid #132d5329;box-shadow:inset 0 1px #ffffff73}[data-dsh-float] [data-dsh-surface]:hover:not(:disabled){background:color-mix(in srgb, #fff calc(74% * var(--dsh-aqua-surface-frost,1)), transparent)}[data-dsh-float] body[data-ds-dark-theme] [data-dsh-surface]{background:color-mix(in srgb, #2a2e38 calc(62% * var(--dsh-aqua-surface-frost,1)), transparent);border-color:#94b4dc33;box-shadow:inset 0 1px #ffffff14}[data-dsh-float] body[data-ds-dark-theme] [data-dsh-surface]:hover:not(:disabled){background:color-mix(in srgb, #363a46 calc(70% * var(--dsh-aqua-surface-frost,1)), transparent)}[data-dsh-float] [role=menuitem],[data-dsh-float] [role=tooltip],[data-dsh-float] [class*=pill]{border-radius:8px}[data-dsh-float] button[class*=button]{border-radius:10px}[data-dsh-float] [class*=iconButton],[data-dsh-float] [class*=searchButton]{border-radius:8px}[data-dsh-float] [data-dsh-add]{background:color-mix(in srgb, #fff calc(40% * var(--dsh-aqua-frost,1)), transparent);backdrop-filter:blur(var(--dsh-aqua-blur,14px));border:1px solid #132d532e;box-shadow:inset 0 1px #ffffff80}[data-dsh-float] [data-dsh-add]:hover:not(:disabled){background:color-mix(in srgb, #fff calc(58% * var(--dsh-aqua-frost,1)), transparent)}[data-dsh-float] body[data-ds-dark-theme] [data-dsh-add]{background:color-mix(in srgb, #2a2e38 calc(40% * var(--dsh-aqua-frost,1)), transparent);border-color:#94b4dc40;box-shadow:inset 0 1px #ffffff14}[data-dsh-float] body[data-ds-dark-theme] [data-dsh-add]:hover:not(:disabled){background:color-mix(in srgb, #363a46 calc(52% * var(--dsh-aqua-frost,1)), transparent)}[data-dsh-float] [class*=bubble]{background:color-mix(in srgb, #fff calc(42% * var(--dsh-aqua-frost,1)), transparent);backdrop-filter:blur(var(--dsh-aqua-blur,14px));border:1px solid #132d5324;border-radius:14px}[data-dsh-float] body[data-ds-dark-theme] [class*=bubble]{background:color-mix(in srgb, #000 calc(40% * var(--dsh-aqua-frost,1)), transparent);border-color:#94b4dc24}html[data-dsh-float][data-dsh-aqua-wallpaper][data-dsh-aqua-media=video] [class*=bubble]{background:#ffffffb3;border-color:#132d5333}html[data-dsh-float][data-dsh-aqua-wallpaper][data-dsh-aqua-media=video] body[data-ds-dark-theme] [class*=bubble]{background:#00000080;border-color:#94b4dc38}[data-dsh-float] [class*=card]{border-radius:14px}[data-dsh-float] [data-composer-card],[data-dsh-float] [data-composer-card]:after{border-radius:24px}[data-dsh-float] [data-composer-card]{z-index:8;background:var(--dsh-aqua-glass-card-light);backdrop-filter:blur(var(--dsh-aqua-blur,14px));border:1px solid #132d5342;position:relative;box-shadow:inset 0 1px #ffffff80,0 10px 36px #132d5329}[data-dsh-float] body[data-ds-dark-theme] [data-composer-card]{background:var(--dsh-aqua-glass-card-dark);border:1px solid #94b4dc52;box-shadow:inset 0 1px #ffffff12,0 10px 36px #02060e80}[data-dsh-aqua][data-dsh-float] [data-dsh-inputbar]:has([data-dsh-stats]){width:calc(var(--dsh-chat-content-width) + 32px);background:var(--dsh-aqua-glass-card-light);max-width:none;backdrop-filter:blur(var(--dsh-aqua-blur,14px));border:1px solid #132d5342;border-radius:24px;margin:0 auto 12px;padding:0;box-shadow:inset 0 1px #ffffff80,0 10px 36px #132d5329}[data-dsh-float] body[data-ds-dark-theme] [data-dsh-inputbar]:has([data-dsh-stats]){background:var(--dsh-aqua-glass-card-dark);border-color:#94b4dc52;box-shadow:inset 0 1px #ffffff12,0 10px 36px #02060e80}[data-dsh-float] [data-dsh-inputbar]:has([data-dsh-stats]) [data-composer-card]{box-shadow:none;backdrop-filter:none;background:0 0;border:none;border-radius:0}[data-dsh-float] [data-dsh-inputbar]:has([data-dsh-stats]) [data-composer-card]:after{display:none}[data-dsh-float] [data-dsh-inputbar]:has([data-dsh-stats]) [data-dsh-stats]{box-sizing:border-box;width:100%;max-width:none;min-height:24px;box-shadow:none;backdrop-filter:none;background:0 0;border:none;border-top:1px solid #132d532e;border-radius:0;margin:auto 0 0;padding:2px 16px;display:block}[data-dsh-float] body[data-ds-dark-theme] [data-dsh-inputbar]:has([data-dsh-stats]) [data-dsh-stats]{border-top-color:#94b4dc3d}[data-dsh-float] [data-composer-card]:after{-webkit-mask:url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3Crect width='100%25' height='100%25' fill='none' rx='24' ry='24' stroke='black' stroke-width='2' stroke-dasharray='4 4'/%3E%3C/svg%3E\");mask:url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3Crect width='100%25' height='100%25' fill='none' rx='24' ry='24' stroke='black' stroke-width='2' stroke-dasharray='4 4'/%3E%3C/svg%3E\")}[data-dsh-float] [class*=block]{--dsl-code-block-border-radius:14px;--dsl-diff-radius:14px;--dsl-read-radius:14px;--dsl-terminal-radius:14px;--dsl-web-radius:14px;--dsl-search-radius:14px;border-radius:14px}[data-dsh-float] [data-phase] > header{background:var(--dsh-aqua-glass-card-light);backdrop-filter:blur(var(--dsh-aqua-blur,14px));border:1px solid #132d5342;border-bottom-color:#0000;border-radius:20px;margin:12px 16px 0;padding:10px 16px 8px;box-shadow:inset 0 1px #ffffff80,0 10px 34px #132d5329}[data-dsh-float] [data-phase] > header:after{display:none}[data-dsh-float] body[data-ds-dark-theme] [data-phase] > header{background:var(--dsh-aqua-glass-card-dark);border-color:#94b4dc52 #94b4dc52 #0000;box-shadow:inset 0 1px #ffffff12,0 8px 30px #02060e52}[data-dsh-float] [data-dsh-frame][data-sidebar-collapsed] [data-phase] > header{margin-left:28px}[data-dsh-float] [class*=sidebarCol]{z-index:9;background:var(--dsh-aqua-glass-card-light);backdrop-filter:blur(var(--dsh-aqua-blur,14px));border:1px solid #132d5342;border-right-color:#96bef5a6;border-radius:20px;margin:12px;padding:10px 12px 14px;position:relative;overflow:hidden;box-shadow:inset 0 1px #ffffff80,0 10px 34px #132d5329}[data-dsh-float] body[data-ds-dark-theme] [class*=sidebarCol]{background:var(--dsh-aqua-glass-card-dark);border-color:#94b4dc52 #94b4dc33 #94b4dc52 #94b4dc52;border-right-style:solid;border-right-width:1px;box-shadow:inset 0 1px #ffffff12,0 8px 30px #02060e52}[data-dsh-float] [data-dsh-frame][data-sidebar-collapsed] [class*=sidebarCol]{transition:margin .15s var(--ds-ease-in-out), border-radius .15s var(--ds-ease-in-out), transform .1s ease-out;border-radius:16px;margin:12px -12px 12px 12px;padding:0}[data-dsh-float] [data-dsh-frame]:not([data-sidebar-collapsed]) [data-dsh-sidebar-root]{width:100%!important}[data-dsh-float] [data-dsh-trajectory]{background:var(--dsh-aqua-glass-card-light);width:calc(100% - 32px);height:calc(100% - 20px);backdrop-filter:blur(var(--dsh-aqua-blur,14px));border:1px solid #132d5342;border-radius:20px;margin:8px 16px 12px;overflow:hidden;box-shadow:inset 0 1px #ffffff80,0 10px 34px #132d5329}[data-dsh-float] body[data-ds-dark-theme] [data-dsh-trajectory]{background:var(--dsh-aqua-glass-card-dark);border-color:#94b4dc52;box-shadow:inset 0 1px #ffffff12,0 8px 30px #02060e52}[data-dsh-float] [data-dsh-trajectory] [role=toolbar],[data-dsh-float] [data-dsh-trajectory] section[aria-label=Trajectory\\ timeline]{background:0 0}[data-dsh-float] [data-dsh-inputbar]:not([class*=hero]){padding-bottom:12px}[data-dsh-float] [data-dsh-stats]{z-index:8;width:calc(var(--dsh-chat-content-width) + 32px);background:var(--dsh-aqua-glass-card-light);max-width:none;backdrop-filter:blur(var(--dsh-aqua-blur,14px));color:#262e3ee6;border:1px solid #132d5342;border-top-color:#132d532e;border-radius:0 0 24px 24px;margin:0 auto;padding:2px 16px;position:relative;box-shadow:0 10px 36px #132d5329}[data-dsh-float] body[data-ds-dark-theme] [data-dsh-stats]{background:var(--dsh-aqua-glass-card-dark);color:#e4ecf8eb;border-color:#94b4dc3d #94b4dc52 #94b4dc52;box-shadow:0 10px 36px #02060e80}[data-dsh-float] [data-composer-card] textarea::placeholder{color:#37405480}[data-dsh-float] body[data-ds-dark-theme] [data-composer-card] textarea::placeholder{color:#cdd8ea85}[data-dsh-aqua][data-dsh-aqua-spotlight] [data-dsh-aqua-spot]{isolation:isolate;position:relative}[data-dsh-aqua] [data-dsh-aqua-glow]{display:none}[data-dsh-aqua][data-dsh-aqua-spotlight] [data-dsh-aqua-glow]{border-radius:inherit;pointer-events:none;opacity:0;z-index:-1;transition:opacity .3s;display:block;position:absolute;inset:0}[data-dsh-aqua][data-dsh-aqua-spotlight] [data-dsh-aqua-spot][data-spot-on] [data-dsh-aqua-glow]{opacity:1}[data-dsh-aqua][data-dsh-aqua-spotlight] [data-dsh-inputbar][data-dsh-aqua-spot] [data-dsh-aqua-glow]{border-radius:24px}[data-dsh-aqua][data-dsh-float] [class*=sidebarCol]:has([role=dialog]){backdrop-filter:none}[data-dsh-float][data-dsh-aqua-press] [data-dsh-aqua-spot]{transition:transform .1s ease-out}[data-dsh-aqua] body [role=dialog]{backdrop-filter:blur(50px);background:#ffffff73}[data-dsh-aqua] body[data-ds-dark-theme] [role=dialog]{background:#111a278c}[data-dsh-float] [role=treeitem][aria-selected=true]{box-shadow:inset 2px 0 0 var(--dsw-specific-sidebar-nav-item-active-accent), 0 0 16px #6e9be824}[data-dsh-float] button[class*=button]:hover:not(:disabled),[data-dsh-float] [role=menuitem]:hover:not(:disabled){box-shadow:0 0 12px #6e9be829,inset 0 0 0 1px #94b4dc38}[data-dsh-float] [role=menu]{background:color-mix(in srgb, #fff calc(62% * var(--dsh-aqua-frost,1)), transparent);backdrop-filter:blur(var(--dsh-aqua-blur,14px))}[data-dsh-float] body[data-ds-dark-theme] [role=menu]{background:color-mix(in srgb, #1c202a calc(68% * var(--dsh-aqua-frost,1)), transparent)}[data-dsh-aqua] [data-dsh-aqua-fade]{z-index:7;pointer-events:none;backdrop-filter:blur(5px);background:#fff3;height:13px;position:fixed;left:0;right:0}[data-dsh-aqua] body[data-ds-dark-theme] [data-dsh-aqua-fade]{background:#00000026}[data-dsh-aqua] [data-dsh-aqua-fade=top]{top:0;-webkit-mask-image:linear-gradient(#000 0%,#0000 100%);mask-image:linear-gradient(#000 0%,#0000 100%)}[data-dsh-aqua] [data-dsh-aqua-fade=bottom]{bottom:0;-webkit-mask-image:linear-gradient(#0000 0%,#000 100%);mask-image:linear-gradient(#0000 0%,#000 100%)}[data-dsh-aqua] :focus-visible{outline-offset:1px;outline:2px solid #6e9be8d9}[data-dsh-aqua] ::selection{background:#6e9be859}[data-dsh-aqua] [data-conversation-scroll]{text-shadow:0 0 1px #0006}[data-dsh-aqua] body:not([data-ds-dark-theme]) [data-conversation-scroll]{text-shadow:0 0 1px #ffffff8c,0 1px 2px #132d5314}[data-dsh-float] [role=dialog] h2{letter-spacing:.02em;font-family:var(--owa-font-stack,sans-serif);font-weight:600}[data-dsh-float] [role=treeitem]{font-family:var(--owa-font-stack,sans-serif);font-weight:500}[data-dsh-float] [data-phase=hero]{animation:qRUgUq_dsh-aqua-hero-in .32s var(--ds-ease-in-out)}[data-dsh-float] [data-phase=active]{animation:qRUgUq_dsh-aqua-active-in .3s var(--ds-ease-in-out)}[data-dsh-float] [data-testid^=view-]{animation:qRUgUq_dsh-aqua-view-in .26s var(--ds-ease-in-out)}[data-dsh-float] [class*=userRow]{animation:qRUgUq_dsh-aqua-rise .28s var(--ds-ease-in-out) both}[data-dsh-float] [data-tool]{animation:qRUgUq_dsh-aqua-rise .3s var(--ds-ease-in-out) both}[data-dsh-float] [role=dialog]{animation:qRUgUq_dsh-aqua-dialog-in .24s var(--ds-ease-in-out)}@keyframes qRUgUq_dsh-aqua-hero-in{0%{opacity:0}}@keyframes qRUgUq_dsh-aqua-active-in{0%{opacity:0}}@keyframes qRUgUq_dsh-aqua-view-in{0%{opacity:0}}@keyframes qRUgUq_dsh-aqua-rise{0%{opacity:0;transform:translateY(6px)}}@keyframes qRUgUq_dsh-aqua-dialog-in{0%{opacity:0;transform:translateY(8px)scale(.985)}}[data-dsh-compat] [role=menu],[data-dsh-compat] [role=tooltip],[data-dsh-compat] [class*=card],[data-dsh-compat] [class*=bubble],[data-dsh-compat] [class*=panel],[data-dsh-compat] [class*=popover],[data-dsh-compat] [class*=dropdown]{backdrop-filter:blur(12px)}@media (prefers-reduced-motion:reduce){[data-dsh-float] [data-phase=hero],[data-dsh-float] [data-phase=active],[data-dsh-float] [data-testid^=view-],[data-dsh-float] [class*=userRow],[data-dsh-float] [data-tool],[data-dsh-float] [role=dialog],[data-dsh-aqua] [data-dsh-aqua-ambient]{animation:none}}";
		/** The macOS shell paints center and right columns separately from frame.
		* Scope the clear layers to those seams; images, charts and state colors keep
		* their own fills. All materials disappear with the Aqua attribute. */
		const desktopGlassCss = `
html[data-dsh-aqua] body {
  --dsh-aqua-glass-card-light: color-mix(in srgb, #e3efff calc(48% + 18% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-glass-card-dark: color-mix(in srgb, #162c44 calc(54% + 18% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-pane: var(--dsh-aqua-glass-card-light);
  --dsh-aqua-material-sidebar: color-mix(in srgb, #e3efff calc(18% + 12% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-sidebar-blur: min(10px, calc(var(--dsh-aqua-blur, 14px) * .45));
  --dsh-aqua-sidebar-shadow: inset 0 1px rgba(255, 255, 255, .42), 0 6px 22px rgba(19, 45, 83, .08);
  --dsh-aqua-material-read: color-mix(in srgb, #e8f2ff calc(64% + 12% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-strong: color-mix(in srgb, #dfecfd calc(68% + 12% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-control: color-mix(in srgb, #d7e7fc calc(64% + 12% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-input: color-mix(in srgb, #edf5ff calc(78% + 10% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-selected: color-mix(in srgb, #c4ddff 82%, transparent);
  --dsh-aqua-material-overlay: color-mix(in srgb, #e1edfc calc(80% + 8% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-highlight: rgba(255, 255, 255, .42);
}
html[data-dsh-aqua] body[data-ds-dark-theme] {
  --dsh-aqua-material-pane: var(--dsh-aqua-glass-card-dark);
  --dsh-aqua-material-sidebar: color-mix(in srgb, #162c44 calc(24% + 14% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-sidebar-shadow: inset 0 1px rgba(197, 224, 255, .12), 0 6px 22px rgba(2, 6, 14, .18);
  --dsh-aqua-material-read: color-mix(in srgb, #172e46 calc(68% + 12% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-strong: color-mix(in srgb, #213952 calc(72% + 12% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-control: color-mix(in srgb, #244260 calc(64% + 12% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-input: color-mix(in srgb, #10263c calc(80% + 10% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-selected: color-mix(in srgb, #315d8e 84%, transparent);
  --dsh-aqua-material-overlay: color-mix(in srgb, #142c44 calc(82% + 8% * var(--dsh-aqua-frost, 1)), transparent);
  --dsh-aqua-material-highlight: rgba(197, 224, 255, .12);
}
/* Unknown image/video contrast needs the reading veil; fluid uses a thinner
   sidebar lens so the same whole-window flow continues through its edge. */
html[data-dsh-aqua][data-dsh-aqua-wallpaper] body {
  --dsh-aqua-material-sidebar: var(--dsh-aqua-material-pane);
}
html[data-dsh-aqua] [data-dsh-frame],
html[data-dsh-aqua] [data-dsh-frame] > :is([class*="_centerCol"], [data-rightbar-col]),
html[data-dsh-aqua] [data-dsh-sidebar-root],
html[data-dsh-aqua] [data-phase]:has([data-conversation-content]),
html[data-dsh-aqua] [data-conversation-region="composer"][class*="_composerSeat"] {
  background: transparent !important;
}
html[data-dsh-aqua] [data-dsh-frame] > [class*="_sidebarCol"] {
  background: var(--dsh-aqua-material-sidebar) !important;
  border-color: var(--dsw-alias-border-l2);
  backdrop-filter: blur(var(--dsh-aqua-sidebar-blur)) saturate(112%);
  box-shadow: var(--dsh-aqua-sidebar-shadow);
}
/* The stock SidebarRoot paints sidebar-fill outside macOS. Clear this exact
   structural child as well as the stamped root, retaining row/selection fills. */
html[data-dsh-aqua] [data-dsh-sidebar-root],
html[data-dsh-aqua] [data-dsh-frame] > [class*="_sidebarCol"] > [class*="_root"] {
  background: transparent !important;
  backdrop-filter: none !important;
  box-shadow: none !important;
}
html[data-dsh-aqua] [data-phase] > header,
html[data-dsh-aqua] [data-dsh-trajectory] {
  background: var(--dsh-aqua-material-pane) !important;
}
html[data-dsh-aqua] body [role="dialog"] {
  background: var(--dsh-aqua-material-overlay) !important;
  color: var(--dsw-alias-label-primary);
  backdrop-filter: blur(max(18px, var(--dsh-aqua-blur, 14px))) saturate(125%);
}
html[data-dsh-aqua] [data-shortcut-modal="settings"] > nav,
html[data-dsh-aqua] [data-shortcut-modal="settings"] > [class*="_content"],
html[data-dsh-aqua] [data-shortcut-modal="settings"] [class*="_header"] {
  background: transparent;
}
html[data-dsh-aqua] [data-composer-card] :is(textarea, [contenteditable]) {
  color: var(--dsw-alias-label-primary);
}
html[data-dsh-aqua] [data-composer-card] textarea::placeholder {
  color: var(--dsw-alias-label-secondary);
}
@media (prefers-reduced-transparency: reduce), (prefers-contrast: more) {
  html[data-dsh-aqua] body {
    --dsh-aqua-glass-card-light: #e3efff;
    --dsh-aqua-glass-card-dark: #162c44;
    --dsh-aqua-material-sidebar: #e3efff;
    --dsh-aqua-material-read: #e8f2ff;
    --dsh-aqua-material-strong: #dfecfd;
    --dsh-aqua-material-control: #d7e7fc;
    --dsh-aqua-material-input: #edf5ff;
    --dsh-aqua-material-overlay: #e1edfc;
  }
  html[data-dsh-aqua] body[data-ds-dark-theme] {
    --dsh-aqua-material-sidebar: #162c44;
    --dsh-aqua-material-read: #172e46;
    --dsh-aqua-material-strong: #213952;
    --dsh-aqua-material-control: #244260;
    --dsh-aqua-material-input: #10263c;
    --dsh-aqua-material-overlay: #142c44;
  }
  html[data-dsh-aqua] body :is([data-dsh-frame] > [class*="_sidebarCol"], [data-composer-card], [role="dialog"]) {
    backdrop-filter: none;
  }
}
@media (forced-colors: active) {
  html[data-dsh-aqua] body :is([data-dsh-frame] > [class*="_sidebarCol"], [data-composer-card], [role="dialog"]) {
    background: Canvas !important;
    color: CanvasText;
    border: 1px solid CanvasText;
    box-shadow: none;
    backdrop-filter: none;
  }
}`;
		const tagId$1 = "dsh-client-ui-aqua/aqua.module.css";
		if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId$1) + "]") === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = "dsh-client-ui-aqua";
			tag.dataset.pluginCss = tagId$1;
			tag.textContent = css$1;
			tag.textContent += desktopGlassCss;
			document.head.appendChild(tag);
		}
		//#endregion
		//#region src/client/index.ts
		/** Required services: theme override stack plus the settings-card surfaces. */
		const inject = [
			"theme",
			"slots",
			"locale"
		];
		/**
		* Client plugin body.
		* @param ctx - client cordis context.
		*/
		function apply(ctx) {
			const t = ctx.locale.bind(NS);
			ctx.effect(() => ctx.locale.register(NS, {
				zh,
				en
			}), "ui-aqua: settings dictionaries");
			const layer = new AquaLayer(ctx);
			const appearanceStore = createAquaRowStore();
			let appearanceBound;
			let revision = 0;
			const payload = () => {
				const s = layer.getSettings();
				return {
					enabled: layer.getEnabled(),
					mode: s.mode,
					blur: s.blur,
					frost: s.frost,
					ontologySurfaceStrength: s.ontologySurfaceStrength,
					fluidHue: s.fluidHue,
					fluidDepth: s.fluidDepth,
					bgBrightness: s.bgBrightness,
					dark: layer.getDark(),
					background: s.background,
					wallpaper: s.wallpaper,
					mesh: s.mesh,
					spotlight: s.spotlight,
					press: s.press,
					wallpaperBlur: s.wallpaperBlur,
					wallpaperFrost: s.wallpaperFrost,
					videoBlur: s.videoBlur,
					videoBrightness: s.videoBrightness,
					mediaError: layer.mediaError
				};
			};
			const sync = () => {
				const next = payload();
				appearanceBound?.sync(next, revision);
				revision += 1;
			};
			layer.onMediaChange = sync;
			ctx.effect(() => ctx.on("theme/change", () => {
				sync();
			}), "ui-aqua: appearance scheme sync");
			const appearanceInjected = (actions) => {
				appearanceBound = actions;
				sync();
				return {
					setEnabled: (enabled) => {
						layer.setEnabled(enabled);
						sync();
					},
					setMode: (mode) => {
						layer.setMode(mode);
						sync();
					},
					setBlur: (blur) => {
						layer.setBlur(blur);
						sync();
					},
					setFrost: (frost) => {
						layer.setFrost(frost);
						sync();
					},
					setOntologySurfaceStrength: (ontologySurfaceStrength) => {
						layer.setOntologySurfaceStrength(ontologySurfaceStrength);
						sync();
					},
					setFluidHue: (fluidHue) => {
						layer.setFluidHue(fluidHue);
						sync();
					},
					setFluidDepth: (fluidDepth) => {
						layer.setFluidDepth(fluidDepth);
						sync();
					},
					setBgBrightness: (bgBrightness) => {
						layer.setBgBrightness(bgBrightness);
						sync();
					},
					setBackground: (background) => {
						layer.setBackground(background);
						sync();
					},
					setWallpaper: (wallpaper) => {
						layer.setWallpaper(wallpaper);
						sync();
					},
					setMesh: (mesh) => {
						layer.setMesh(mesh);
						sync();
					},
					setSpotlight: (spotlight) => {
						layer.setSpotlight(spotlight);
						sync();
					},
					setPress: (press) => {
						layer.setPress(press);
						sync();
					},
					setWallpaperBlur: (wallpaperBlur) => {
						layer.setWallpaperBlur(wallpaperBlur);
						sync();
					},
					setWallpaperFrost: (wallpaperFrost) => {
						layer.setWallpaperFrost(wallpaperFrost);
						sync();
					},
					setVideoBlur: (videoBlur) => {
						layer.setVideoBlur(videoBlur);
						sync();
					},
					setVideoBrightness: (videoBrightness) => {
						layer.setVideoBrightness(videoBrightness);
						sync();
					},
					authorizeVideo: () => {
						layer.authorizeVideo();
					}
				};
			};
			ctx.slots.inject("settings.plugins.tab", () => ctx.slots.register({
				name: "settings.plugins.tab",
				id: "appearance",
				order: 20,
				label: () => t("aqua.tab"),
				store: appearanceStore,
				locale: NS,
				inject: appearanceInjected
			}, AquaPluginTab));
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map

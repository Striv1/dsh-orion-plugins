(() => {
  if (window.__ORION_DOCUMENT_FILE_POLICY__) return;

  const supportedExtensions = new Set([
    ".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".tsv",
    ".txt", ".md", ".html", ".htm", ".xml", ".json", ".yaml", ".yml", ".eml",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
  ]);
  const systemNames = new Set([".ds_store", "thumbs.db", "desktop.ini"]);

  const extension = (name = "") => {
    const normalized = String(name).trim().toLowerCase();
    const position = normalized.lastIndexOf(".");
    return position >= 0 ? normalized.slice(position) : "";
  };

  const descriptor = (file) => {
    const path = String(file?.webkitRelativePath || file?.name || "未命名文件");
    const name = path.split("/").at(-1) || path;
    const normalizedName = name.toLowerCase();
    const systemFile = systemNames.has(normalizedName) || normalizedName.startsWith("._");
    return {
      name,
      path,
      extension: extension(name) || "无扩展名",
      reason: systemFile ? "系统文件" : "格式暂不支持",
      size: Number(file?.size || 0),
      lastModified: Number(file?.lastModified || 0),
    };
  };

  const supported = (file) => {
    const item = descriptor(file);
    return item.reason !== "系统文件" && supportedExtensions.has(item.extension);
  };

  const signature = (file) => {
    const item = descriptor(file);
    return `${item.path}\u0000${item.size}\u0000${item.lastModified}`;
  };

  const merge = (currentFiles = [], incomingFiles = [], currentSkipped = []) => {
    const files = [...currentFiles];
    const skipped = [...currentSkipped];
    const fileSignatures = new Set(files.map(signature));
    const skippedSignatures = new Set(skipped.map((item) => `${item.path}\u0000${item.size}\u0000${item.lastModified}`));
    for (const file of incomingFiles) {
      if (supported(file)) {
        const key = signature(file);
        if (!fileSignatures.has(key)) {
          fileSignatures.add(key);
          files.push(file);
        }
        continue;
      }
      const item = descriptor(file);
      const key = `${item.path}\u0000${item.size}\u0000${item.lastModified}`;
      if (!skippedSignatures.has(key)) {
        skippedSignatures.add(key);
        skipped.push(item);
      }
    }
    return { files, skipped };
  };

  window.__ORION_DOCUMENT_FILE_POLICY__ = {
    descriptor,
    merge,
    supported,
    supportedExtensions: [...supportedExtensions],
  };
})();

/** File-picker identity is only used to avoid adding the same file twice. */
export function mergeUploadFiles(existing: File[], incoming: File[]) {
  const key = (file: File) => JSON.stringify([file.name, file.size, file.lastModified]);
  const seen = new Set(existing.map(key));
  const files = [...existing];
  let duplicates = 0;
  for (const file of incoming) {
    const identity = key(file);
    if (seen.has(identity)) duplicates++;
    else {
      seen.add(identity);
      files.push(file);
    }
  }
  return { files, duplicates };
}

const hashes = new WeakMap<File, Promise<string>>();
/** Before submitting, compare real content, including renamed copies. */
export function pdfFingerprint(file: File): Promise<string> {
  let hash = hashes.get(file);
  if (!hash) {
    hash = file.arrayBuffer().then(async (bytes) => {
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      return [...new Uint8Array(digest)]
        .map((v) => v.toString(16).padStart(2, "0"))
        .join("");
    });
    hashes.set(file, hash);
  }
  return hash;
}

// A random per-browser id. The backend hashes it with the user agent into the device fingerprint
// the guard uses to block bots, so real shoppers keep the same id across visits.
const KEY = "cg_device_id";

export function getDeviceId(): string {
  try {
    const existing = localStorage.getItem(KEY);
    if (existing) return existing;
    const id = `web-${crypto.randomUUID()}`;
    localStorage.setItem(KEY, id);
    return id;
  } catch {
    return `web-ephemeral-${Math.random().toString(36).slice(2, 12)}`;
  }
}

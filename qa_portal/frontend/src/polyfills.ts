// crypto.randomUUID needs a secure context; the VPN edge serves plain HTTP.
// Same fix as web_chat/frontend/src/polyfills.ts — kept independent per
// worker scope (forbidden to import across app boundaries).
if (typeof crypto.randomUUID !== "function") {
  Object.defineProperty(crypto, "randomUUID", {
    value: () => {
      const b = crypto.getRandomValues(new Uint8Array(16));
      b[6] = (b[6] & 0x0f) | 0x40;
      b[8] = (b[8] & 0x3f) | 0x80;
      const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    },
    configurable: true,
    writable: true,
  });
}

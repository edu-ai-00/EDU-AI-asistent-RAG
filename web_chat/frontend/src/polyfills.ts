// crypto.randomUUID is gated behind a secure context, and the VPN edge serves
// plain HTTP — so assistant-ui's internal id generator throws and the app never
// mounts. getRandomValues carries no such gate, so this stays a real v4 UUID.
if (typeof crypto.randomUUID !== "function") {
  Object.defineProperty(crypto, "randomUUID", {
    value: () => {
      const b = crypto.getRandomValues(new Uint8Array(16));
      b[6] = (b[6] & 0x0f) | 0x40; // version 4
      b[8] = (b[8] & 0x3f) | 0x80; // variant 1
      const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    },
    configurable: true,
    writable: true,
  });
}

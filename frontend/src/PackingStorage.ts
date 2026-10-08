// Only the employee's last open IDs are retained; server records remain authoritative.
export const packingStorage = {
  get(key: string): string | null { try { return sessionStorage.getItem(key); } catch { return null; } },
  set(key: string, value: string): void { try { sessionStorage.setItem(key, value); } catch { /* Navigation still works when browser storage is unavailable. */ } },
  remove(key: string): void { try { sessionStorage.removeItem(key); } catch { /* Nothing to restore. */ } },
};

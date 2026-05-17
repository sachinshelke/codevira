// TypeScript client for the polyglot fixture.
export interface HealthResponse {
  status: string;
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch('/health');
  return res.json();
}

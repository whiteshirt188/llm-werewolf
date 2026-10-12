export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}
export async function request<T>(path: string, body?: unknown, extraHeaders?: Record<string, string>): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin', signal: controller.signal,
      headers: body === undefined ? extraHeaders : { 'Content-Type': 'application/json', ...extraHeaders },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const data: unknown = await response.json();
    if (!response.ok) {
      const detail = (data as {detail?: unknown}).detail;
      throw new ApiError(response.status, typeof detail === 'string' ? detail : `请求不符合要求（HTTP ${response.status}）。`);
    }
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new Error('暂时连接不到后端。请确认 make api 或 make play 仍在运行；操作结果可能已保存，请先刷新局面。');
  } finally { clearTimeout(timer); }
}

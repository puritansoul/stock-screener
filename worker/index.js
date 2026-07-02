export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = url.searchParams.get('url');
    if (!target) return new Response('Missing url param', { status: 400 });
    const resp = await fetch(target, {
      headers: { 'User-Agent': 'Mozilla/5.0' }
    });
    const body = await resp.text();
    return new Response(body, {
      headers: {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
      }
    });
  }
}

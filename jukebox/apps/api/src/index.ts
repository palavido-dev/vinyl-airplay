import Fastify from "fastify";
import cors from "@fastify/cors";
import { ensureSchema } from "./lib/migrate.js";
import { auth } from "./lib/auth.js";
import { productRoutes } from "./routes/product.js";

ensureSchema();

const app = Fastify({ logger: true });

await app.register(cors, {
  origin: true,
  credentials: true,
});

await app.register(productRoutes);

app.route({
  method: ["GET", "POST"],
  url: "/api/auth/*",
  async handler(request, reply) {
    const url = new URL(request.url, `http://${request.headers.host}`);
    const headers = new Headers();
    for (const [key, value] of Object.entries(request.headers)) {
      if (value) headers.append(key, Array.isArray(value) ? value.join(",") : value);
    }
    const req = new Request(url.toString(), {
      method: request.method,
      headers,
      body:
        request.method !== "GET" && request.method !== "HEAD"
          ? JSON.stringify(request.body)
          : undefined,
    });
    const response = await auth.handler(req);
    reply.status(response.status);
    response.headers.forEach((v, k) => reply.header(k, v));
    const text = await response.text();
    return reply.send(text || null);
  },
});

const port = Number(process.env.PORT ?? 8787);
const host = process.env.HOST ?? "0.0.0.0";

try {
  await app.listen({ port, host });
  console.log(`[api] Vinyl Jukebox API on http://${host}:${port}`);
} catch (err) {
  app.log.error(err);
  process.exit(1);
}

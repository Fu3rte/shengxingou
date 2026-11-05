import type { NextConfig } from "next"

const nextConfig: NextConfig = {
  devIndicators: false,
  turbopack: { root: process.cwd() },
  async rewrites() {
    return [{ source: "/api/:path*", destination: "http://127.0.0.1:8765/api/:path*" }]
  },
}

export default nextConfig

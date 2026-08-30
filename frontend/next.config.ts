import type { NextConfig } from "next";

const projectRoot = process.cwd();

const nextConfig: NextConfig = {
  // Produces a minimal server plus its traced runtime dependencies for the
  // final Docker stage; public/ and .next/static are copied beside it there.
  output: "standalone",
  // This repository has lockfiles above the frontend directory. Pin both
  // roots here so local and container builds emit the same standalone layout.
  outputFileTracingRoot: projectRoot,
  turbopack: {
    root: projectRoot,
  },
  images: {
    unoptimized: true,
  },
  devIndicators: false,
};

export default nextConfig;

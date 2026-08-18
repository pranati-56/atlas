import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `pg` and the PDF/DOCX parsers reach for Node built-ins and dynamic requires.
  // Bundling them into the server chunk breaks those requires; leaving them
  // external makes Next `require()` them at runtime instead.
  serverExternalPackages: ["pg", "unpdf", "mammoth"],

  eslint: {
    // Type errors gate the build via `npm run typecheck`; lint runs separately
    // in CI so a style nit cannot block a deploy.
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;

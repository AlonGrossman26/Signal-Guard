import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Verdict colours, used by the live decision feed.
        approve: "#16a34a",
        reject: "#dc2626",
      },
    },
  },
  plugins: [],
};

export default config;

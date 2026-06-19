/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg:    "#0b0f17",
        panel: "#111824",
        panel2: "#1a2333",
        border: "#212b3d",
        accent: "#60a5fa",
        good:  "#10b981",
        warn:  "#f59e0b",
        bad:   "#ef4444",
        mute:  "#94a3b8",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Monaco", "monospace"],
      },
    },
  },
  plugins: [],
};

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#eef4ff",
          500: "#3b6ef6",
          600: "#2c56d4",
          700: "#2242a8",
        },
      },
    },
  },
  plugins: [],
};

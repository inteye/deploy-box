/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/templates/**/*.html",
  ],
  theme: {
    extend: {
      colors: {
        sand: {
          50: "#fbfaf6",
          100: "#f5f4ef",
          200: "#e9e6dc",
          300: "#d8d3c5",
          400: "#bcb4a2",
          500: "#9f9582",
          600: "#817867",
          700: "#686052",
          800: "#514b42",
          900: "#403b35",
        },
        pine: {
          50: "#eef5f1",
          100: "#dceae2",
          200: "#bfd7c9",
          300: "#96bea7",
          400: "#68a083",
          500: "#478467",
          600: "#376d55",
          700: "#2f5c49",
          800: "#294b3d",
          900: "#233e34",
        }
      },
      boxShadow: {
        shell: "0 24px 64px rgba(43, 55, 48, 0.14)",
        soft: "0 18px 48px rgba(43, 55, 48, 0.075)",
      },
      borderRadius: {
        "4xl": "2rem",
      },
      gridTemplateColumns: {
        "workspace": "minmax(200px, 220px) minmax(0, 1fr) minmax(280px, 300px)",
      }
    },
  },
  plugins: [],
};

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/templates/**/*.html",
  ],
  theme: {
    extend: {
      colors: {
        sand: {
          50: "#fffcf7",
          100: "#faf3e7",
          200: "#eedfc4",
          300: "#dbc29a",
          400: "#c9a46d",
          500: "#b78b4d",
          600: "#9f7139",
          700: "#825a31",
          800: "#694a2d",
          900: "#563e28",
        },
        pine: {
          50: "#effaf6",
          100: "#d8f1e6",
          200: "#b5e2cf",
          300: "#82cfb0",
          400: "#4cb589",
          500: "#26976b",
          600: "#177b57",
          700: "#116347",
          800: "#0f4f3a",
          900: "#0d4231",
        }
      },
      boxShadow: {
        shell: "0 18px 40px rgba(55, 37, 12, 0.08)",
        soft: "0 12px 28px rgba(55, 37, 12, 0.06)",
      },
      borderRadius: {
        "4xl": "2rem",
      },
      gridTemplateColumns: {
        "workspace": "minmax(220px, 260px) minmax(0, 1fr) minmax(320px, 380px)",
      }
    },
  },
  plugins: [],
};

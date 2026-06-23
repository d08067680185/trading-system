/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        surface: '#0f1117',
        card: '#1a1d27',
        border: '#2d3142',
      },
    },
  },
  plugins: [],
}

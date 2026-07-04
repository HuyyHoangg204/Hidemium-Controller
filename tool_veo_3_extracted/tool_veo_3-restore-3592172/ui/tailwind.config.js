/** @type {import('tailwindcss').Config} */
module.exports = {
    darkMode: 'class',
    content: ["./dist/**/*.{html,js}"],
    theme: {
        extend: {
            colors: {
                dark: {
                    DEFAULT: '#f8fafc',
                    paper: '#ffffff',
                    primary: '#6366f1',
                    accent: '#8b5cf6'
                }
            }
        }
    },
    plugins: [],
}

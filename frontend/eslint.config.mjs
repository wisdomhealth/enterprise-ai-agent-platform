import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

const eslintConfig = [
  { ignores: [".next/**", "coverage/**", "node_modules/**", "playwright-report/**"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default eslintConfig;

"use client";

import { AnimatedThemeToggler } from "@/components/ui/animated-theme-toggler";

export function ThemeToggle() {
  return (
    <AnimatedThemeToggler
      aria-label="切换主题"
      title="切换主题"
      variant="circle"
      className="inline-flex size-10 shrink-0 items-center justify-center rounded-xl text-stone-500 transition-colors duration-200 hover:bg-stone-100 hover:text-stone-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-stone-400 dark:text-stone-300 dark:hover:bg-white/10 dark:hover:text-white [&_svg]:size-4"
    />
  );
}

"use client";

import { ThemeToggle } from "@/components/theme-toggle";
import { cn } from "@/lib/utils";

export function HeaderActions({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center", className)}>
      <ThemeToggle />
    </div>
  );
}

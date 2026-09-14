import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "../utils/cx";

export type RuntimeSectionTone = "base" | "soft" | "paper" | "volt";
export type RuntimeSectionWidth = "lg" | "xl" | "none";

export type RuntimeSectionProps = HTMLAttributes<HTMLElement> & {
  children: ReactNode;
  containerClassName?: string;
  tone?: RuntimeSectionTone;
  width?: RuntimeSectionWidth;
  withGrid?: boolean;
};

const toneClasses: Record<RuntimeSectionTone, string> = {
  base: "border-b border-runtime-line bg-runtime-bg text-ink",
  soft: "border-b border-runtime-line bg-runtime-soft text-ink",
  paper: "border-b border-ink-paper-line bg-ink-paper text-runtime-bg",
  volt: "border-b border-runtime-mint-line bg-brand-volt bg-volt-grid bg-volt-grid-size text-runtime-bg",
};

const widthClasses: Record<RuntimeSectionWidth, string> = {
  lg: "mx-auto w-full max-w-runtime-lg px-4 sm:px-6",
  xl: "mx-auto w-full max-w-runtime-xl px-4 sm:px-6",
  none: "",
};

export function RuntimeSection({
  children,
  className,
  containerClassName,
  tone = "base",
  width = "lg",
  withGrid = false,
  ...props
}: RuntimeSectionProps) {
  return (
    <section className={cx("py-16 md:py-24", toneClasses[tone], withGrid && (tone === "base" || tone === "soft") && "bg-runtime-grid bg-runtime-grid-size", className)} {...props}>
      <div className={cx(widthClasses[width], containerClassName)}>{children}</div>
    </section>
  );
}

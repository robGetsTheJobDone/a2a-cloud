import type { AnchorHTMLAttributes, ButtonHTMLAttributes, ReactNode } from "react";
import { cx } from "../utils/cx";

export type ButtonVariant = "primary" | "accent" | "ghost" | "volt";
export type ButtonSize = "sm" | "md";

type ButtonBaseProps = {
  children: ReactNode;
  className?: string;
  size?: ButtonSize;
  variant?: ButtonVariant;
};

type AnchorButtonProps = ButtonBaseProps &
  Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "className" | "children"> & {
    href: string;
  };

type NativeButtonProps = ButtonBaseProps &
  Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className" | "children"> & {
    href?: undefined;
  };

export type ButtonProps = AnchorButtonProps | NativeButtonProps;

const variantClasses: Record<ButtonVariant, string> = {
  primary: "border-ink bg-ink text-runtime-bg hover:bg-ink-soft",
  accent:
    "border-signal-protocol-strong/35 bg-signal-protocol-strong/10 text-signal-protocol hover:border-signal-protocol-strong/60 hover:bg-signal-protocol-strong/15",
  ghost: "border-runtime-line-mid bg-transparent text-ink-soft hover:border-runtime-mint-line hover:bg-brand-volt/[0.06]",
  volt: "border-brand-volt bg-brand-volt text-runtime-bg hover:bg-brand-field-hover",
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: "min-h-9 px-3 py-2 text-runtime-xs",
  md: "min-h-10 px-4 py-2.5 text-runtime-sm",
};

const baseClasses =
  "inline-flex items-center justify-center gap-2 rounded-runtime-md border font-mono font-medium leading-none transition-colors duration-150 ease-telemetry disabled:pointer-events-none disabled:opacity-40";

export function Button(props: ButtonProps) {
  const { className, size = "md", variant = "primary" } = props;
  const classes = cx(baseClasses, sizeClasses[size], variantClasses[variant], className);

  if ("href" in props && props.href) {
    const { children, className: _className, size: _size, variant: _variant, ...anchorProps } = props;
    return (
      <a className={classes} {...anchorProps}>
        {children}
      </a>
    );
  }

  const { children, className: _className, size: _size, type, variant: _variant, ...buttonProps } = props as NativeButtonProps;
  const buttonType: ButtonHTMLAttributes<HTMLButtonElement>["type"] = type ?? "button";

  return (
    <button className={classes} type={buttonType} {...buttonProps}>
      {children}
    </button>
  );
}

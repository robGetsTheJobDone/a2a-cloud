import type { HTMLAttributes, ReactNode } from "react";
import { Dialog, EmptyState } from "./DashboardChrome";

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

/**
 * ListDetailLayout — the dashboard's pervasive list+detail shell.
 *
 * Two panes: a scrollable left list rail of fixed/resizable width and a right
 * content pane. Keep-alive friendly: it NEVER conditionally unmounts its
 * children. The empty state (when no `detail` is supplied) is layered via CSS
 * over a persistent detail container so selection state, scroll position, and
 * subscriptions inside the detail subtree survive a transient empty render.
 */
export type ListDetailLayoutProps = HTMLAttributes<HTMLDivElement> & {
  list: ReactNode;
  detail: ReactNode;
  /** CSS width for the list rail (any valid grid-template length). Default 320px. */
  listWidth?: string;
  /** Rendered in the detail pane when `detail` is falsy. Kept mounted, hidden via CSS. */
  empty?: ReactNode;
  listLabel?: string;
  listClassName?: string;
  detailClassName?: string;
};

export function ListDetailLayout({
  list,
  detail,
  listWidth = "320px",
  empty,
  listLabel = "List",
  className,
  listClassName,
  detailClassName,
  style,
  ...divProps
}: ListDetailLayoutProps) {
  const hasDetail = Boolean(detail);
  const emptyNode =
    empty ?? <EmptyState title="Nothing selected" description="Choose an item from the list to view its detail." />;

  return (
    <div
      {...divProps}
      style={{ ["--list-detail-rail" as string]: listWidth, ...style }}
      className={cx(
        "grid min-h-0 min-w-0 flex-1 overflow-hidden",
        "grid-rows-[minmax(0,1fr)] lg:grid-cols-[var(--list-detail-rail)_minmax(0,1fr)]",
        className,
      )}
    >
      <aside
        aria-label={listLabel}
        className={cx(
          "min-h-0 min-w-0 overflow-y-auto border-b border-runtime-line-soft/70 bg-runtime-bg lg:border-b-0 lg:border-r",
          listClassName,
        )}
      >
        {list}
      </aside>
      <div className={cx("relative min-h-0 min-w-0 overflow-hidden bg-runtime-bg", detailClassName)}>
        {/* Persistent detail subtree: never unmounted, hidden (not removed) when empty. */}
        <div
          aria-hidden={!hasDetail}
          hidden={!hasDetail}
          className="h-full min-h-0 min-w-0 overflow-y-auto"
        >
          {detail}
        </div>
        {/* Empty layer: occupies the pane only while there is no detail. */}
        <div
          aria-hidden={hasDetail}
          hidden={hasDetail}
          className="flex h-full min-h-0 min-w-0 items-center justify-center overflow-y-auto p-6"
        >
          {emptyNode}
        </div>
      </div>
    </div>
  );
}

/**
 * DualPaneResourceBrowser — generic list+detail browser built on
 * ListDetailLayout. Drives selection: renders one row per item in the rail and
 * the selected item's detail on the right. Behavior-preserving wrapper — it
 * owns no async state; selection is controlled via `selectedId`/`onSelect`.
 */
export type DualPaneResourceBrowserProps<T> = {
  items: T[];
  selectedId: string | null | undefined;
  onSelect: (id: string, item: T) => void;
  getId: (item: T) => string;
  renderRow: (item: T, ctx: { selected: boolean; onSelect: () => void }) => ReactNode;
  renderDetail: (item: T) => ReactNode;
  emptyState?: ReactNode;
  /** Optional sticky toolbar pinned above the list rail. */
  toolbar?: ReactNode;
  listWidth?: string;
  listLabel?: string;
  className?: string;
  listClassName?: string;
  detailClassName?: string;
};

export function DualPaneResourceBrowser<T>({
  items,
  selectedId,
  onSelect,
  getId,
  renderRow,
  renderDetail,
  emptyState,
  toolbar,
  listWidth,
  listLabel = "Resources",
  className,
  listClassName,
  detailClassName,
}: DualPaneResourceBrowserProps<T>) {
  const selectedItem =
    (selectedId != null && items.find((item) => getId(item) === selectedId)) || null;

  const list = (
    <div className="flex min-h-0 min-w-0 flex-col">
      {toolbar && (
        <div className="sticky top-0 z-10 border-b border-runtime-line-soft/70 bg-runtime-bg/95 px-3 py-2 backdrop-blur">
          {toolbar}
        </div>
      )}
      {items.length === 0 ? (
        <div className="p-3">
          {emptyState ?? (
            <EmptyState size="compact" title="No items" description="Nothing to show yet." />
          )}
        </div>
      ) : (
        <ul role="list" className="flex flex-col gap-1.5 p-2">
          {items.map((item) => {
            const id = getId(item);
            const selected = id === selectedId;
            return (
              <li key={id} className="min-w-0">
                {renderRow(item, { selected, onSelect: () => onSelect(id, item) })}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );

  return (
    <ListDetailLayout
      className={className}
      listWidth={listWidth}
      listLabel={listLabel}
      listClassName={listClassName}
      detailClassName={detailClassName}
      list={list}
      detail={selectedItem ? renderDetail(selectedItem) : null}
      empty={emptyState}
    />
  );
}

/**
 * DetailSheet — canonical mandate-C detail/edit surface. A thin wrapper over the
 * existing Dialog with placement="right" (from DashboardChrome). Feature
 * surfaces open detail/edit IN PLACE as a right-side sheet, never as a
 * full-page route redirect.
 */
export type DetailSheetProps = {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  description?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg" | "xl";
  closeLabel?: string;
};

export function DetailSheet({
  open,
  onClose,
  title,
  children,
  description,
  footer,
  size = "md",
  closeLabel,
}: DetailSheetProps) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      placement="right"
      size={size}
      closeLabel={closeLabel}
      actions={footer}
    >
      {children}
    </Dialog>
  );
}

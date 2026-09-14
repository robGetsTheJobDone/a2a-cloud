import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  DetailSheet,
  DualPaneResourceBrowser,
  ListDetailLayout,
} from "./ListDetailLayout";

describe("ListDetailLayout", () => {
  it("renders both the list rail and the detail pane", () => {
    const html = renderToStaticMarkup(
      <ListDetailLayout list={<div>rail-content</div>} detail={<div>detail-content</div>} />,
    );

    expect(html).toContain("rail-content");
    expect(html).toContain("detail-content");
  });

  it("keeps the detail subtree mounted and hides it via CSS when empty", () => {
    const html = renderToStaticMarkup(
      <ListDetailLayout
        list={<div>rail</div>}
        detail={null}
        empty={<div>nothing-here</div>}
      />,
    );

    expect(html).toContain("nothing-here");
    // Detail subtree stays in the markup even with no detail: the empty layer
    // and the (hidden) detail layer both render.
    expect(html).toContain("aria-hidden");
  });

  it("applies a custom rail width via the CSS variable", () => {
    const html = renderToStaticMarkup(
      <ListDetailLayout list={<div>rail</div>} detail={<div>d</div>} listWidth="280px" />,
    );

    expect(html).toContain("--list-detail-rail:280px");
  });
});

describe("DualPaneResourceBrowser", () => {
  type Item = { id: string; name: string };
  const items: Item[] = [
    { id: "a", name: "Alpha" },
    { id: "b", name: "Bravo" },
  ];

  it("renders a row per item and the selected item's detail", () => {
    const html = renderToStaticMarkup(
      <DualPaneResourceBrowser<Item>
        items={items}
        selectedId="b"
        onSelect={() => undefined}
        getId={(item) => item.id}
        renderRow={(item, { selected }) => (
          <span>
            {item.name}
            {selected ? " *" : ""}
          </span>
        )}
        renderDetail={(item) => <div>detail:{item.name}</div>}
      />,
    );

    expect(html).toContain("Alpha");
    expect(html).toContain("Bravo *");
    expect(html).toContain("detail:Bravo");
  });

  it("falls back to the empty state when no item is selected", () => {
    const html = renderToStaticMarkup(
      <DualPaneResourceBrowser<Item>
        items={items}
        selectedId={null}
        onSelect={() => undefined}
        getId={(item) => item.id}
        renderRow={(item) => <span>{item.name}</span>}
        renderDetail={(item) => <div>detail:{item.name}</div>}
        emptyState={<div>pick-one</div>}
      />,
    );

    expect(html).toContain("pick-one");
    expect(html).not.toContain("detail:Alpha");
  });

  it("renders a toolbar when provided", () => {
    const html = renderToStaticMarkup(
      <DualPaneResourceBrowser<Item>
        items={items}
        selectedId="a"
        onSelect={() => undefined}
        getId={(item) => item.id}
        renderRow={(item) => <span>{item.name}</span>}
        renderDetail={(item) => <div>{item.name}</div>}
        toolbar={<div>toolbar-slot</div>}
      />,
    );

    expect(html).toContain("toolbar-slot");
  });
});

describe("DetailSheet", () => {
  it("renders nothing when closed", () => {
    const html = renderToStaticMarkup(
      <DetailSheet open={false} onClose={() => undefined} title="Edit">
        <div>sheet-body</div>
      </DetailSheet>,
    );

    expect(html).toBe("");
  });

  it("renders as a right-side sheet with title, body, and footer when open", () => {
    const html = renderToStaticMarkup(
      <DetailSheet
        open
        onClose={() => undefined}
        title="Edit resource"
        footer={<button type="button">Save</button>}
      >
        <div>sheet-body</div>
      </DetailSheet>,
    );

    expect(html).toContain("Edit resource");
    expect(html).toContain("sheet-body");
    expect(html).toContain("Save");
    // placement="right" => the side-sheet entry animation class.
    expect(html).toContain("sheet-in-right");
  });
});

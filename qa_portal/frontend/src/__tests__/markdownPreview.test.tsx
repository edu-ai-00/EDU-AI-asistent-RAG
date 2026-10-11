import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MarkdownPreview } from "../components/MarkdownPreview";

afterEach(cleanup);

describe("MarkdownPreview — honest about dropped HTML", () => {
  it("renders ordinary markdown (bold, link) without the stripped-HTML notice", () => {
    render(<MarkdownPreview source={"**tučně** viz https://example.com/info"} />);
    const strong = screen.getByText("tučně");
    expect(strong.tagName).toBe("STRONG");
    const link = screen.getByRole("link", { name: "https://example.com/info" });
    expect(link.getAttribute("href")).toBe("https://example.com/info");
    expect(screen.queryByTestId("markdown-html-stripped")).toBeNull();
  });

  it("shows the notice for an <img onerror=...> tag and renders no executable markup", () => {
    render(<MarkdownPreview source={'<img src=x onerror="alert(1)">'} />);
    expect(screen.getByTestId("markdown-html-stripped")).toBeTruthy();
    const { container } = render(<MarkdownPreview source={'<img src=x onerror="alert(1)">'} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
  });

  it("shows the notice for a solo <script> block and renders no script/iframe/onerror", () => {
    const { container } = render(<MarkdownPreview source={"<script>alert(1)</script>"} />);
    expect(screen.getByTestId("markdown-html-stripped")).toBeTruthy();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
  });

  it("does not show the notice for 'a < b' or a bare numeric comparison", () => {
    render(<MarkdownPreview source={"a < b and 5 < 10"} />);
    expect(screen.queryByTestId("markdown-html-stripped")).toBeNull();
  });

  it("does not show the notice for an e-mail written in angle brackets", () => {
    render(<MarkdownPreview source={"Kontakt: <jana.novakova@example.com>"} />);
    expect(screen.queryByTestId("markdown-html-stripped")).toBeNull();
  });

  it("does not show the notice for inline code containing a tag-like string", () => {
    render(<MarkdownPreview source={"Use `<div>` for a container."} />);
    expect(screen.queryByTestId("markdown-html-stripped")).toBeNull();
  });

  it("does not show the notice for a fenced code block containing HTML", () => {
    const source = ["```html", "<div class=\"x\"><span>hi</span></div>", "```"].join("\n");
    render(<MarkdownPreview source={source} />);
    expect(screen.queryByTestId("markdown-html-stripped")).toBeNull();
  });

  it("shows the notice for an HTML comment (e.g. pasted from Word)", () => {
    render(<MarkdownPreview source={"Termín <!-- ověřit u ředitele --> je předběžný."} />);
    expect(screen.getByTestId("markdown-html-stripped")).toBeTruthy();
  });

  it("shows the notice for a DOCTYPE declaration", () => {
    render(<MarkdownPreview source={"<!DOCTYPE html>\n<p>text</p>"} />);
    expect(screen.getByTestId("markdown-html-stripped")).toBeTruthy();
  });

  it("shows the notice for a CDATA section", () => {
    render(<MarkdownPreview source={"<![CDATA[x]]>"} />);
    expect(screen.getByTestId("markdown-html-stripped")).toBeTruthy();
  });

  it("does not show the notice for a fenced code block containing an HTML comment", () => {
    const source = ["```", "<!-- x -->", "```"].join("\n");
    render(<MarkdownPreview source={source} />);
    expect(screen.queryByTestId("markdown-html-stripped")).toBeNull();
  });

  it("does not show the notice for 'cena < 100 Kč' or an e-mail in angle brackets", () => {
    render(<MarkdownPreview source={"cena < 100 Kč, kontakt <foo@bar.cz>"} />);
    expect(screen.queryByTestId("markdown-html-stripped")).toBeNull();
  });

  it("still renders the link text for a javascript: URL, but strips the dangerous href", () => {
    // rehype-sanitize drops the whole `href` for a disallowed scheme, so the
    // anchor loses its implicit "link" role — assert on the anchor directly.
    const { container } = render(<MarkdownPreview source={"[klikni](javascript:alert(1))"} />);
    const anchor = container.querySelector("a");
    expect(anchor).not.toBeNull();
    expect(anchor?.textContent).toBe("klikni");
    const href = anchor?.getAttribute("href") ?? "";
    expect(href.startsWith("javascript:")).toBe(false);
  });
});

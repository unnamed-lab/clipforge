import { expect, test } from "@playwright/test";

test.describe("app shell", () => {
  test("the homepage renders the header and hero", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByRole("banner")).toBeVisible();
    await expect(page.getByRole("link", { name: "Clipforge home" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Find the clips");
    await expect(page.getByRole("link", { name: "Import a video" })).toHaveAttribute("href", "/new");
  });

  test("primary navigation reaches the templates and settings pages", async ({ page }) => {
    await page.goto("/");
    const nav = page.getByRole("navigation", { name: "Main" }).first();

    await nav.getByRole("link", { name: "Templates" }).click();
    await expect(page).toHaveURL(/\/templates$/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Recipes");

    await nav.getByRole("link", { name: "Settings" }).click();
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Local engine");
  });

  test("unknown routes render the 404 page", async ({ page }) => {
    const response = await page.goto("/no-such-cut");

    expect(response?.status()).toBe(404);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("This cut doesn't exist");
    await expect(page.getByRole("link", { name: "Back to projects" })).toHaveAttribute("href", "/");
  });
});

test.describe("mobile shell", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("the menu reaches every section and closes after navigating", async ({ page }) => {
    await page.goto("/");

    const toggle = page.getByRole("button", { name: "Open menu" });
    await expect(toggle).toBeVisible();
    await toggle.click();

    const menu = page.locator("#mobile-nav");
    await expect(menu).toBeVisible();
    await expect(menu.getByRole("link", { name: "Settings" })).toBeVisible();

    await menu.getByRole("link", { name: "Templates" }).click();
    await expect(page).toHaveURL(/\/templates$/);
    await expect(page.locator("#mobile-nav")).toHaveCount(0);
  });

  test("no route scrolls sideways at 390px", async ({ page }) => {
    for (const path of ["/", "/templates", "/new", "/settings", "/no-such-cut"]) {
      await page.goto(path);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      );
      expect(overflow, `${path} overflows the viewport horizontally`).toBeLessThanOrEqual(1);
    }
  });
});

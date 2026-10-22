const { test, expect } = require('@playwright/test');
test('watchlist, trades, self-tests and persistence', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#watchlist tr')).toHaveCount(15);
  await expect(page.locator('#cash')).toHaveText('$1,000.00');
  await page.locator('#symbol').selectOption('SOFI');
  await page.locator('#quantity').fill('2');
  await page.getByRole('button',{name:'Execute simulated trade'}).click();
  await expect(page.locator('#cash')).toHaveText('$976.00');
  await page.reload();
  await expect(page.locator('#cash')).toHaveText('$976.00');
  await page.locator('#side').selectOption('sell');
  await page.locator('#symbol').selectOption('SOFI');
  await page.locator('#quantity').fill('2');
  await page.getByRole('button',{name:'Execute simulated trade'}).click();
  await expect(page.locator('#cash')).toHaveText('$1,000.00');
  await page.getByRole('button',{name:'Run GUI self-tests'}).click();
  await expect(page.locator('#testStatus')).toContainText('PASS Browser storage roundtrip');
  await expect(page.locator('#testStatus')).not.toContainText('FAIL');
});
test('invalid overspend rejected',async ({page})=>{
 await page.goto('/');
 await page.locator('#symbol').selectOption('ASML');
 await page.locator('#quantity').fill('2');
 await page.getByRole('button',{name:'Execute simulated trade'}).click();
 await expect(page.locator('#tradeStatus')).toContainText('Insufficient demo cash');
});

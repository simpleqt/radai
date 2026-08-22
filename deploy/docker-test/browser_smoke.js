#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const FULL_SHA = /^[0-9a-f]{40}$/;

function parseArguments(argv) {
  const options = { targets: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    const value = argv[index + 1];
    if (!value) throw new Error(`missing value for ${argument}`);
    if (argument === '--git-sha') options.gitSha = value;
    else if (argument === '--output') options.output = value;
    else if (argument === '--target') options.targets.push(value);
    else throw new Error(`unknown argument: ${argument}`);
    index += 1;
  }
  if (!FULL_SHA.test(options.gitSha || '')) throw new Error('invalid --git-sha');
  if (!options.output) throw new Error('missing --output');
  if (options.targets.length !== 6) throw new Error('exactly six --target values are required');
  return options;
}

function atomicJson(outputPath, payload) {
  const temporary = `${outputPath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, outputPath);
  fs.chmodSync(outputPath, 0o600);
}

async function checkAuditWorkspace(page, baseUrl, initialResponse) {
  const auditUrl = new URL('/audit', baseUrl).href;
  const sidebarText = (await page.locator('aside').innerText()).trim();
  const bodyText = (await page.locator('body').innerText()).trim();
  const navigation = page.locator('aside nav');
  const navigationRegionCount = await navigation.count();
  const navigationText = navigationRegionCount ? (await navigation.innerText()).trim() : '';
  const navigationButtonCount = await navigation.getByRole('button').count();
  const auditLogo = page.locator('img[alt="高校审计智能体"]');
  const auditLogoVisible = await auditLogo.isVisible();
  const auditLogoState = await auditLogo.evaluate((element) => ({
    complete: element.complete,
    naturalWidth: element.naturalWidth,
    naturalHeight: element.naturalHeight,
  }));
  const auditLogoLoaded = Boolean(
    auditLogoState.complete
    && auditLogoState.naturalWidth > 0
    && auditLogoState.naturalHeight > 0
  );
  const forbiddenLabels = ['探索式学习', '自定义学习', '学习风格', '专家·技能·连接器', '个人文件库'];
  const leakedLabels = forbiddenLabels.filter((label) => navigationText.includes(label));
  const restrictedRouteResults = [];
  for (const route of ['/app', '/admin', '/explore']) {
    const requestedUrl = new URL(route, baseUrl).href;
    const response = await page.goto(requestedUrl, { waitUntil: 'networkidle', timeout: 30000 });
    restrictedRouteResults.push({
      requested_url: requestedUrl,
      final_url: page.url(),
      http_status: response ? response.status() : 0,
      redirected_to_audit: new URL(page.url()).pathname === '/audit',
    });
  }
  const passed = Boolean(
    initialResponse && initialResponse.ok()
    && new URL(initialResponse.url()).pathname === '/audit'
    && sidebarText.includes('高校审计智能助手')
    && sidebarText.includes('新建问答')
    && sidebarText.includes('问答记录')
    && bodyText.includes('你好，我是高校审计智能助手')
    && bodyText.includes('高校科研经费审计重点有哪些？')
    && !sidebarText.includes('知识问答')
    && navigationRegionCount === 0
    && navigationButtonCount === 0
    && auditLogoVisible
    && auditLogoLoaded
    && leakedLabels.length === 0
    && restrictedRouteResults.every((item) => (
      item.http_status === 200 && item.redirected_to_audit
    ))
  );
  return {
    requested_url: baseUrl,
    expected_url: auditUrl,
    initial_final_url: initialResponse ? initialResponse.url() : '',
    http_status: initialResponse ? initialResponse.status() : 0,
    navigation_region_count: navigationRegionCount,
    navigation_button_count: navigationButtonCount,
    audit_logo_visible: auditLogoVisible,
    audit_logo_loaded: auditLogoLoaded,
    audit_logo_natural_size: {
      width: auditLogoState.naturalWidth,
      height: auditLogoState.naturalHeight,
    },
    leaked_navigation_labels: leakedLabels,
    restricted_route_results: restrictedRouteResults,
    status: passed ? 'passed' : 'failed',
  };
}

async function checkTarget(browser, encodedTarget) {
  const separator = encodedTarget.indexOf('=');
  if (separator < 1) throw new Error(`invalid target: ${encodedTarget}`);
  const name = encodedTarget.slice(0, separator);
  const url = encodedTarget.slice(separator + 1);
  const parsed = new URL(url);
  if (parsed.protocol !== 'http:' || parsed.hostname !== '127.0.0.1') {
    throw new Error(`browser target must use isolated host loopback: ${url}`);
  }

  const page = await browser.newPage();
  const pageErrors = [];
  const consoleErrors = [];
  const requestFailures = [];
  const badResponses = [];
  const loadedScripts = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('requestfailed', (request) => {
    requestFailures.push({ url: request.url(), failure: request.failure() });
  });
  page.on('response', (response) => {
    if (response.request().resourceType() === 'script' && response.ok()) {
      loadedScripts.push(response.url());
    }
    if (response.status() >= 400) {
      badResponses.push({ url: response.url(), status: response.status() });
    }
  });

  try {
    const response = await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(500);
    const title = (await page.title()).trim();
    const bodyText = (await page.locator('body').innerText()).trim();
    const status = response ? response.status() : 0;
    const auditWorkspace = name === 'audit-frontend'
      ? await checkAuditWorkspace(page, url, response)
      : null;
    const passed = Boolean(
      response && response.ok() && title && bodyText.length >= 20 && loadedScripts.length
      && !pageErrors.length && !consoleErrors.length && !requestFailures.length
      && !badResponses.length
      && (!auditWorkspace || auditWorkspace.status === 'passed')
    );
    return {
      name,
      requested_url: url,
      final_url: page.url(),
      http_status: status,
      title,
      body_characters: bodyText.length,
      loaded_script_count: loadedScripts.length,
      page_errors: pageErrors,
      console_errors: consoleErrors,
      request_failures: requestFailures,
      bad_responses: badResponses,
      audit_workspace: auditWorkspace,
      status: passed ? 'passed' : 'failed',
    };
  } finally {
    await page.close();
  }
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  const startedAt = Math.floor(Date.now() / 1000);
  const report = {
    format_version: 1,
    status: 'failed',
    git_sha: options.gitSha,
    started_at_unix: startedAt,
    completed_at_unix: startedAt,
    engine: 'playwright-chromium',
    results: [],
  };

  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    // 五个前端各占独立回环端口，一浏览器内并发开页（Promise.all 保序），
    // 冒烟耗时按最慢端点而非五端点之和计。
    report.results = await Promise.all(
      options.targets.map((target) => checkTarget(browser, target)),
    );
    report.status = report.results.every((item) => item.status === 'passed')
      ? 'passed'
      : 'failed';
  } catch (error) {
    report.error = `${error.name || 'Error'}: ${error.message || String(error)}`;
  } finally {
    if (browser) await browser.close();
    report.completed_at_unix = Math.floor(Date.now() / 1000);
    fs.mkdirSync(path.dirname(options.output), { recursive: true, mode: 0o700 });
    atomicJson(options.output, report);
  }

  console.log(JSON.stringify(report, null, 2));
  return report.status === 'passed' ? 0 : 1;
}

main()
  .then((status) => { process.exitCode = status; })
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });

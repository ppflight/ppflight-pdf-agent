#!/usr/bin/env php
<?php

declare(strict_types=1);

use PPFlight\InvoiceRenderer\InvoiceRenderer;
use PPFlight\InvoiceRenderer\RenderException;
use PPFlight\InvoiceRenderer\SnapshotValidator;

const ROOT = __DIR__ . '/..';
const LARAVEL_AUTOLOAD = '/www/wwwroot/www.ppflight.com/laravel/vendor/autoload.php';

$autoload = is_file(ROOT . '/renderer/vendor/autoload.php')
    ? ROOT . '/renderer/vendor/autoload.php'
    : LARAVEL_AUTOLOAD;
if (!is_file($autoload)) {
    throw new RuntimeException('Install renderer dependencies before running this test.');
}
require $autoload;
require ROOT . '/renderer/src/RenderException.php';
require ROOT . '/renderer/src/SnapshotValidator.php';
require ROOT . '/renderer/src/InvoiceRenderer.php';

function expect(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function rejects(callable $test, string $message): void
{
    try {
        $test();
    } catch (RenderException) {
        return;
    }
    throw new RuntimeException($message);
}

function fixture(): array
{
    $json = file_get_contents(__DIR__ . '/renderer_fixture.json');
    if ($json === false) {
        throw new RuntimeException('Fixture is unavailable.');
    }
    return json_decode($json, true, 64, JSON_THROW_ON_ERROR);
}

function validate(array $snapshot): void
{
    (new SnapshotValidator())->decodeAndValidate(json_encode($snapshot, JSON_THROW_ON_ERROR));
}

function decompressedPdfStreams(string $pdf): string
{
    preg_match_all('~stream\r?\n(.*?)\r?\nendstream~s', $pdf, $matches);
    $streams = '';
    foreach ($matches[1] as $stream) {
        $decoded = @gzuncompress($stream);
        if ($decoded !== false) {
            $streams .= $decoded;
        }
    }
    return $streams;
}

$snapshot = fixture();
validate($snapshot);
$invalidDisplay = $snapshot;
$invalidDisplay['invoice']['display_number'] = 'PPFlight Cloud';
rejects(static fn () => validate($invalidDisplay), 'Unsafe invoice display number was accepted.');
$font = ROOT . '/renderer/assets/PPFlightSansSC-Regular.ttf';
expect(is_file($font) && filesize($font) > 0, 'Chinese font asset is unavailable.');
$fontHash = hash_file('sha256', $font);
expect(is_string($fontHash) && preg_match('/^[a-f0-9]{64}$/', $fontHash) === 1, 'Chinese font hash is invalid.');

$unknown = $snapshot;
$unknown['html'] = '<b>not allowed</b>';
rejects(static fn () => validate($unknown), 'Unknown top-level HTML field was accepted.');
$unknown = $snapshot;
$unknown['issuer']['website'] = 'https://evil.example';
rejects(static fn () => validate($unknown), 'External issuer URL was accepted.');
$unknown = $snapshot;
$unknown['issuer']['company_name'] = '<script>alert(1)</script>';
rejects(static fn () => validate($unknown), 'Issuer HTML was accepted.');
$unknown = $snapshot;
$unknown['line_items'][0]['path'] = '/etc/passwd';
rejects(static fn () => validate($unknown), 'Local-path field was accepted.');
$unknown = $snapshot;
$unknown['amounts']['total'] = '1.234';
rejects(static fn () => validate($unknown), 'Non-canonical amount was accepted.');
$unknown = $snapshot;
$unknown['line_items'] = array_fill(0, 251, $snapshot['line_items'][0]);
rejects(static fn () => validate($unknown), '251 line items were accepted.');
$unknown = $snapshot;
$unknown['payments'] = array_fill(0, 101, $snapshot['payments'][0]);
rejects(static fn () => validate($unknown), '101 payments were accepted.');
$unicodeLimit = $snapshot;
$unicodeLimit['customer']['name'] = str_repeat('中', 160);
$unicodeLimit['issuer']['address'] = [str_repeat('地', 191)];
validate($unicodeLimit);
$unicodeLimit['customer']['name'] .= '文';
rejects(static fn () => validate($unicodeLimit), 'Customer-name Unicode character limit was not enforced.');
$unicodeLimit = $snapshot;
$unicodeLimit['issuer']['address'] = [str_repeat('地', 192)];
rejects(static fn () => validate($unicodeLimit), 'Issuer-address Unicode character limit was not enforced.');
rejects(static fn () => (new SnapshotValidator())->decodeAndValidate(str_repeat(' ', SnapshotValidator::MAX_INPUT_BYTES + 1)), 'Input larger than 512 KiB was accepted.');

$xss = $snapshot;
$xss['customer']['name'] = '李伟';
$xss['line_items'][0]['description'] = '<script>window.evil=1</script> · 中国节点';
validate($xss);
$base = sys_get_temp_dir() . '/ppflight-renderer-test-' . bin2hex(random_bytes(6));
mkdir($base, 0700, true);
try {
    $renderer = new InvoiceRenderer(ROOT . '/renderer/assets', $base);
    $htmlMethod = new ReflectionMethod($renderer, 'html');
    $htmlMethod->setAccessible(true);
    $html = $htmlMethod->invoke($renderer, $xss);
    expect(str_contains($html, '&lt;script&gt;window.evil=1&lt;/script&gt;'), 'Untrusted line item was not HTML-escaped.');
    expect(str_contains($html, '李伟') && str_contains($html, '中国节点'), 'Chinese snapshot text was not retained in the fixed template.');
    expect(!str_contains($html, 'http://') && !str_contains($html, 'https://'), 'Fixed template has an external URL.');
    expect(!str_contains($html, 'PPFlight Cloud'), 'Fixed brand must never say PPFlight Cloud.');
    expect(str_contains($html, 'class="header"') && str_contains($html, 'background:#e7e5e4') && str_contains($html, '<img class="brand-mark" src="data:image/svg+xml;base64,'), 'Accepted grey A4 invoice layout or verified brand mark image is missing.');
    expect(!str_contains($html, '#35c8df') && !str_contains(file_get_contents(ROOT . '/renderer/src/InvoiceRenderer.php'), 'date('), 'Renderer contains non-snapshot visual or runtime content.');
    putenv('PPFLIGHT_CJK_FONT_SHA256=' . str_repeat('0', 64));
    $fontPath = new ReflectionMethod($renderer, 'fontPath');
    $fontPath->setAccessible(true);
    rejects(static fn () => $fontPath->invoke($renderer), 'Incorrect configured Chinese font hash was accepted.');
    putenv('PPFLIGHT_CJK_FONT_SHA256');

    $outputPath = $base . '/PPFlight-INV-20260826-1042.pdf';
    $command = [PHP_BINARY, ROOT . '/renderer/bin/render.php', '--output', $outputPath, '--cache-dir', $base];
    $spec = [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
    $process = proc_open($command, $spec, $pipes, ROOT, ['PPFLIGHT_DOMPDF_TEST_AUTOLOAD' => $autoload]);
    expect(is_resource($process), 'Could not start renderer CLI.');
    fwrite($pipes[0], json_encode($xss, JSON_THROW_ON_ERROR));
    fclose($pipes[0]);
    $stdout = stream_get_contents($pipes[1]);
    $stderr = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    expect(proc_close($process) === 0, 'Renderer CLI failed: ' . $stderr);
    $summary = json_decode($stdout, true, 16, JSON_THROW_ON_ERROR);
    $pdf = file_get_contents($outputPath);
    expect($pdf !== false && str_starts_with($pdf, '%PDF-') && strlen($pdf) > 512, 'Output is not a substantive PDF.');
    expect(str_starts_with(basename($outputPath), 'PPFlight-') && !str_contains(basename($outputPath), 'PPFlight Cloud'), 'Download filename does not follow the PPFlight naming rule.');
    expect(array_keys($summary) === ['ok', 'sha256', 'size_bytes'] && $summary['ok'] === true, 'CLI success summary does not match the core contract.');
    expect($summary['size_bytes'] === strlen($pdf) && $summary['sha256'] === hash('sha256', $pdf), 'Output summary does not match PDF.');
    $metadataTitle = "\xFE\xFF" . mb_convert_encoding('PPFlight Invoice INV-20260826-1042', 'UTF-16BE', 'UTF-8');
    $metadataPPFlight = "\xFE\xFF" . mb_convert_encoding('PPFlight', 'UTF-16BE', 'UTF-8');
    expect(str_contains($pdf, '/Title') && str_contains($pdf, $metadataTitle) && str_contains($pdf, '/Creator') && str_contains($pdf, '/Producer') && str_contains($pdf, $metadataPPFlight), 'PDF metadata is incomplete.');
    expect(is_file($base . '/installed-fonts.json'), 'FontMetrics did not create the protected font registry.');
    $pageStreams = decompressedPdfStreams($pdf);
    expect(preg_match('~/Type /Page(?:\s|/)~', $pdf) === 1, 'Rendered PDF has no page object.');
    expect(str_contains($pageStreams, '3.000 5.500 m') && str_contains($pageStreams, '1.7 w 0 J 1 j'), 'Verified PPFlight brand-mark SVG path was not rendered into the PDF page.');
    expect(str_contains($pageStreams, mb_convert_encoding('billing@ppflight.com', 'UTF-16BE', 'UTF-8')), 'Frozen footer email was not rendered into the PDF page.');
    expect(!str_contains($pageStreams, 'PPFlight Cloud'), 'Rendered document contains the forbidden cloud brand.');
    expect((fileperms($outputPath) & 0777) === 0600, 'Output file permissions are not 0600.');
} finally {
    foreach (glob($base . '/*') ?: [] as $file) {
        unlink($file);
    }
    rmdir($base);
}

fwrite(STDOUT, "renderer tests passed\n");

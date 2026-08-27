<?php

declare(strict_types=1);

namespace PPFlight\InvoiceRenderer;

use Dompdf\Dompdf;
use Dompdf\Options;

final class InvoiceRenderer
{
    private const FONT_FAMILY = 'PPFlight Sans SC';
    private const BRAND_MARK_DATA_URI = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyOCAzMiIgZmlsbD0ibm9uZSI+PHBhdGggZD0iTTMgNS41IDI0LjUgMyAxOCAxMS4ySDguOEwxNSAxNmwtNC42IDEyTDYuNSAxNC44IDMgMTEuN2g5LjgiIHN0cm9rZT0iY3VycmVudENvbG9yIiBzdHJva2Utd2lkdGg9IjEuNyIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIvPjwvc3ZnPg==';
    private const MIN_PDF_BYTES = 512;

    public function __construct(
        private readonly string $assetsDirectory,
        private readonly string $cacheDirectory,
    ) {
    }

    /** @param array<string,mixed> $snapshot @return array{ok:true,sha256:string,size_bytes:int} */
    public function render(array $snapshot, string $outputPath): array
    {
        $outputPath = $this->outputPath($outputPath);
        $this->ensureDirectory($this->cacheDirectory, 'cache directory');
        $this->ensureDirectory(dirname($outputPath), 'output directory');
        $fontPath = $this->fontPath();

        $options = new Options();
        $options->setChroot([$this->assetsDirectory]);
        $options->setAllowedProtocols(['file://', 'data://']);
        $options->setIsRemoteEnabled(false);
        $options->setIsPhpEnabled(false);
        $options->setIsJavascriptEnabled(false);
        $options->setIsFontSubsettingEnabled(true);
        $options->setDefaultMediaType('print');
        $options->setDefaultFont(self::FONT_FAMILY);
        $options->setDpi(96);
        $options->setTempDir($this->cacheDirectory);
        $options->setFontDir($this->cacheDirectory);
        $options->setFontCache($this->cacheDirectory);
        $options->setImageByteSizeLimit(1_000_000);

        $dompdf = $this->dompdfWithFont($options, $fontPath);
        $dompdf->setPaper('A4', 'portrait');
        $dompdf->loadHtml($this->html($snapshot), 'UTF-8');
        $dompdf->render();
        $dompdf->addInfo('Title', 'PPFlight Invoice ' . $snapshot['invoice']['display_number']);
        $dompdf->addInfo('Author', 'PPFlight');
        $dompdf->addInfo('Creator', 'PPFlight');
        $dompdf->addInfo('Producer', 'PPFlight');
        $dompdf->addInfo('Subject', 'Invoice ' . $snapshot['invoice']['display_number']);
        $pdf = $dompdf->output();
        $this->validatePdf($pdf);
        $this->atomicWrite($outputPath, $pdf);

        return ['ok' => true, 'sha256' => hash('sha256', $pdf), 'size_bytes' => strlen($pdf)];
    }

    private function dompdfWithFont(Options $options, string $fontPath): Dompdf
    {
        $lock = fopen($this->cacheDirectory . '/font-cache.lock', 'c+b');
        if ($lock === false) {
            throw new RenderException('The PPFlight invoice font cache lock is unavailable.');
        }
        try {
            if (!flock($lock, LOCK_EX)) {
                throw new RenderException('The PPFlight invoice font cache could not be locked.');
            }
            // Dompdf reads installed-fonts.json during construction, while
            // registerFont() can rewrite it. The lock prevents a cold-start
            // peer from consuming a partially written registry.
            $dompdf = new Dompdf($options);
            $registered = $dompdf->getFontMetrics()->registerFont([
                'family' => self::FONT_FAMILY,
                'weight' => 'normal',
                'style' => 'normal',
            ], $fontPath);
            if (!$registered) {
                throw new RenderException('The PPFlight invoice font could not be registered.');
            }
            return $dompdf;
        } finally {
            flock($lock, LOCK_UN);
            fclose($lock);
        }
    }

    private function fontPath(): string
    {
        $font = $this->assetsDirectory . '/PPFlightSansSC-Regular.ttf';
        if (!is_file($font) || is_link($font) || !is_readable($font) || filesize($font) === 0) {
            throw new RenderException('Required PPFlight Sans SC font asset is unavailable.');
        }
        $hash = hash_file('sha256', $font);
        if ($hash === false) {
            throw new RenderException('PPFlight Sans SC font hash could not be calculated.');
        }
        $expectedHash = getenv('PPFLIGHT_CJK_FONT_SHA256');
        if (is_string($expectedHash) && $expectedHash !== '' && !hash_equals(strtolower($expectedHash), $hash)) {
            throw new RenderException('PPFlight Sans SC font hash does not match configuration.');
        }
        return $font;
    }

    private function outputPath(string $outputPath): string
    {
        if ($outputPath === '' || $outputPath[0] !== '/' || str_contains($outputPath, "\0")) {
            throw new RenderException('--output must be an absolute path.');
        }
        $parent = realpath(dirname($outputPath));
        $name = basename($outputPath);
        if ($parent === false || $name === '.' || $name === '..') {
            throw new RenderException('--output parent directory is invalid.');
        }
        $outputPath = $parent . '/' . $name;
        if (is_link($outputPath) || (file_exists($outputPath) && !is_file($outputPath))) {
            throw new RenderException('--output must be a regular file path.');
        }
        return $outputPath;
    }

    private function ensureDirectory(string $directory, string $label): void
    {
        if (!is_dir($directory) || !is_writable($directory) || is_link($directory)) {
            throw new RenderException($label . ' must be a writable, non-symlink directory.');
        }
    }

    private function validatePdf(string $pdf): void
    {
        if (strlen($pdf) < self::MIN_PDF_BYTES || !str_starts_with($pdf, '%PDF-')) {
            throw new RenderException('Dompdf did not return a valid PDF.');
        }
    }

    private function atomicWrite(string $outputPath, string $pdf): void
    {
        $temp = tempnam(dirname($outputPath), '.ppflight-invoice-');
        if ($temp === false) {
            throw new RenderException('Could not create a temporary output file.');
        }
        try {
            if (file_put_contents($temp, $pdf, LOCK_EX) !== strlen($pdf) || !rename($temp, $outputPath)) {
                throw new RenderException('Could not atomically write PDF output.');
            }
            @chmod($outputPath, 0600);
        } finally {
            if (file_exists($temp)) {
                @unlink($temp);
            }
        }
    }

    /** @param array<string,mixed> $snapshot */
    private function html(array $snapshot): string
    {
        $en = $snapshot['locale'] === 'en_US';
        $invoice = $snapshot['invoice'];
        $issuer = $snapshot['issuer'];
        $customer = $snapshot['customer'];
        $amounts = $snapshot['amounts'];
        $currency = $this->e($invoice['currency']);
        $label = static fn (string $enValue, string $zhValue): string => $en ? $enValue : $zhValue;
        $value = static fn (?string $input): string => $input === null ? '—' : $input;

        $lineRows = '';
        foreach ($snapshot['line_items'] as $item) {
            $lineRows .= '<tr><td>' . $this->e($item['description']) . '</td><td>' . $this->e((string) $item['quantity']) . '</td><td>' . $currency . ' ' . $this->money($item['unit_amount']) . '</td><td>' . $currency . ' ' . $this->money($item['total']) . '</td></tr>';
        }
        if ($lineRows === '') {
            $lineRows = '<tr><td class="empty" colspan="4">' . $label('No item detail is available for this invoice.', '该账单暂无项目明细。') . '</td></tr>';
        }

        $fromLines = '<div class="party-line"><strong class="latin">' . $this->e($issuer['company_name']) . '</strong></div>';
        foreach ($issuer['address'] as $line) {
            $fromLines .= '<div class="party-line">' . $this->e($line) . '</div>';
        }
        if ($issuer['website'] !== '') {
            $fromLines .= '<div class="party-line latin">' . $this->e($issuer['website']) . '</div>';
        }
        $fromLines .= '<div class="party-line">' . $this->e($issuer['support_email']) . '</div>';

        $billLines = '';
        if ($customer['company'] !== null) {
            $billLines .= '<div class="party-line">' . $this->e($customer['company']) . '</div>';
        }
        $billLines .= '<div class="party-line"><strong>' . $this->e($customer['name']) . '</strong></div>';
        foreach ($customer['address'] as $line) {
            $billLines .= '<div class="party-line">' . $this->e($line) . '</div>';
        }
        $billLines .= '<div class="party-line">' . $this->e($customer['email']) . '</div>';

        $paymentRows = '';
        foreach ($snapshot['payments'] as $payment) {
            $paymentRows .= '<div class="payment"><span class="latin">' . $this->e($payment['provider']) . '</span> · ' . $this->e($payment['reference']) . ' · ' . $currency . ' ' . $this->money($payment['amount']) . ' · ' . $this->e($payment['paid_at']) . '</div>';
        }
        $payments = $paymentRows === '' ? '' : '<section class="payments"><div class="payments-title">' . $label('PAYMENT REFERENCES', '付款参考') . '</div>' . $paymentRows . '</section>';

        return '<!doctype html><html lang="' . ($en ? 'en' : 'zh-CN') . '"><head><meta charset="utf-8"><style>' . $this->css() . '</style></head><body>'
            . '<table class="header"><tr><td class="brand"><img class="brand-mark" src="' . self::BRAND_MARK_DATA_URI . '" alt=""><span class="brand-name">PPFlight</span></td><td class="invoice-title">INVOICE</td></tr></table>'
            . '<table class="summary"><tr><td><span class="label">' . $label('Reference', '账单编号') . '</span><span class="value">' . $this->e($invoice['display_number']) . '</span></td><td><span class="label">' . $label('Status', '状态') . '</span><span class="value">' . $this->e($invoice['status']) . '</span></td></tr><tr><td><span class="label">' . $label('Issued', '开具日期') . '</span><span class="value">' . $this->e($invoice['issued_at']) . '</span></td><td><span class="label">' . $label('Currency', '币种') . '</span><span class="value">' . $currency . '</span></td></tr><tr><td><span class="label">' . $label('Payment due', '付款截止') . '</span><span class="value">' . $this->e($value($invoice['due_at'])) . '</span></td><td><span class="label">' . $label('Paid at', '付款时间') . '</span><span class="value">' . $this->e($value($invoice['paid_at'])) . '</span></td></tr></table>'
            . '<table class="parties"><tr><td><div class="section-label">' . $label('FROM', '开票方') . '</div>' . $fromLines . '</td><td><div class="section-label">' . $label('BILL TO', '收票方') . '</div>' . $billLines . '</td></tr></table>'
            . '<table class="items"><thead><tr><th>' . $label('DESCRIPTION', '项目说明') . '</th><th>' . $label('UNITS', '数量') . '</th><th>' . $label('UNIT COST', '单价') . '</th><th>' . $label('LINE TOTAL', '小计') . '</th></tr></thead><tbody>' . $lineRows . '</tbody></table>'
            . '<table class="totals"><tr><td>' . $label('Subtotal', '税前金额') . '</td><td>' . $currency . ' ' . $this->money($amounts['subtotal']) . '</td></tr><tr><td>' . $label('Discount', '优惠') . '</td><td>' . $currency . ' ' . $this->money($amounts['discount']) . '</td></tr><tr><td>' . $label('Tax', '税费') . '</td><td>' . $currency . ' ' . $this->money($amounts['tax']) . '</td></tr><tr><td>' . $label('Total', '账单总额') . '</td><td>' . $currency . ' ' . $this->money($amounts['total']) . '</td></tr><tr><td>' . $label('Paid', '已支付') . '</td><td>' . $currency . ' ' . $this->money($amounts['amount_paid']) . '</td></tr><tr class="balance"><td>' . $label('BALANCE DUE', '剩余应付') . '</td><td>' . $currency . ' ' . $this->money($amounts['balance_due']) . '</td></tr></table>'
            . $payments
            . '<footer class="footer"><table class="footer-grid"><tr><td>' . $this->e($issuer['footer_email']) . '</td><td>' . $label('Prepared by PPFlight', '由 PPFlight 生成') . '<br>' . $this->e($invoice['display_number']) . '</td></tr></table></footer></body></html>';
    }

    private function css(): string
    {
        return '@page{margin:16mm 15mm 19mm}*{box-sizing:border-box}body{margin:0;color:#18181b;font-family:"' . self::FONT_FAMILY . '",sans-serif;font-size:10pt;line-height:1.45}.latin{font-family:DejaVu Sans,sans-serif}.header,.summary,.parties,.footer-grid{width:100%;border-collapse:collapse}.header td{padding:0;vertical-align:middle}.brand{width:58%;white-space:nowrap}.brand-mark{width:19px;height:24px;margin-right:8px;vertical-align:middle;color:#111827}.brand-name{color:#111827;font-family:DejaVu Sans,sans-serif;font-size:18pt;font-weight:bold;vertical-align:middle}.invoice-title{font-family:DejaVu Sans,sans-serif;font-size:25pt;font-weight:normal;letter-spacing:2.5pt;text-align:right}.summary{margin-top:25px}.summary td{width:50%;padding:0 0 3px;vertical-align:top}.summary td:nth-child(2){padding-left:22px}.label{color:#52525b;display:inline-block;width:96px;padding-right:8px}.value{color:#18181b}.parties{margin-top:24px}.parties td{width:50%;padding:0;vertical-align:top}.parties td:nth-child(2){padding-left:22px}.section-label{margin-bottom:12px;font-size:9pt;font-weight:normal;letter-spacing:.25pt}.party-line{min-height:16px}.items{width:100%;margin-top:34px;border-collapse:collapse;table-layout:fixed}.items thead{display:table-header-group}.items th{padding:9px 8px;background:#e7e5e4;font-size:8.5pt;font-weight:normal;text-align:left}.items td{padding:10px 8px;border-bottom:1px solid #e4e4e7;vertical-align:top;overflow-wrap:anywhere}.items th:nth-child(1),.items td:nth-child(1){width:55%}.items th:nth-child(2),.items td:nth-child(2){width:11%;text-align:right}.items th:nth-child(3),.items td:nth-child(3){width:17%;text-align:right}.items th:nth-child(4),.items td:nth-child(4){width:17%;text-align:right}.empty{color:#71717a}.totals{width:46%;margin:18px 0 0 auto;border-collapse:collapse}.totals td{padding:2px 0 2px 10px}.totals td:last-child{text-align:right}.totals .balance td{padding-top:8px;padding-bottom:8px;border-top:1.5px solid #18181b;border-bottom:1.5px solid #18181b}.totals .balance td:last-child{font-family:DejaVu Sans,sans-serif;font-weight:bold}.payments{margin-top:25px;page-break-inside:avoid}.payments-title{margin-bottom:6px;font-size:9pt;font-weight:normal}.payment{padding:5px 0;border-bottom:1px solid #e4e4e7;font-size:8.5pt;word-wrap:break-word}.footer{position:fixed;right:0;bottom:-11mm;left:0;color:#71717a;font-size:8.5pt}.footer-grid td{width:50%;padding:0;vertical-align:bottom}.footer-grid td:last-child{text-align:right}';
    }

    /** @param mixed $value */
    private function e($value): string
    {
        return htmlspecialchars((string) $value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
    }

    private function money(string $amount): string
    {
        $negative = str_starts_with($amount, '-');
        $amount = ltrim($amount, '-');
        [$whole, $fraction] = array_pad(explode('.', $amount, 2), 2, '');
        return ($negative ? '-' : '') . $whole . '.' . str_pad($fraction, 2, '0');
    }
}

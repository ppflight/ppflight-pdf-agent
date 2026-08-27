<?php

declare(strict_types=1);

namespace PPFlight\InvoiceRenderer;

/** Validates the only document shape that the fixed invoice template accepts. */
final class SnapshotValidator
{
    public const MAX_INPUT_BYTES = 524288;
    public const MAX_LINE_ITEMS = 250;
    public const MAX_PAYMENTS = 100;

    /** @return array<string, mixed> */
    public function decodeAndValidate(string $json): array
    {
        if ($json === '' || strlen($json) > self::MAX_INPUT_BYTES) {
            throw new RenderException('Snapshot must be between 1 and 524288 bytes.');
        }

        try {
            $snapshot = json_decode($json, true, 64, JSON_THROW_ON_ERROR);
        } catch (\JsonException $exception) {
            throw new RenderException('Snapshot must be valid JSON.', 'input', $exception);
        }

        if (!is_array($snapshot) || array_is_list($snapshot)) {
            throw new RenderException('Snapshot must be a JSON object.');
        }

        $this->keys($snapshot, ['schema_version', 'locale', 'template_version', 'invoice', 'issuer', 'customer', 'line_items', 'payments', 'amounts'], 'snapshot');
        $this->required($snapshot, ['schema_version', 'locale', 'template_version', 'invoice', 'issuer', 'customer', 'line_items', 'payments', 'amounts'], 'snapshot');

        if ($snapshot['schema_version'] !== 1) {
            throw new RenderException('schema_version must be integer 1.');
        }
        if (!in_array($snapshot['locale'], ['en_US', 'zh_CN'], true)) {
            throw new RenderException('locale must be en_US or zh_CN.');
        }
        $this->string($snapshot['template_version'], 'template_version', 64, '/^[A-Za-z0-9][A-Za-z0-9._-]*$/D');

        $invoice = $this->object($snapshot['invoice'], 'invoice');
        $this->keys($invoice, ['display_number', 'issued_at', 'due_at', 'paid_at', 'status', 'currency'], 'invoice');
        $this->required($invoice, ['display_number', 'issued_at', 'due_at', 'paid_at', 'status', 'currency'], 'invoice');
        $this->string($invoice['display_number'], 'invoice.display_number', 64, '/^[A-Za-z0-9][A-Za-z0-9._-]*$/D');
        $this->dateOrNull($invoice['issued_at'], 'invoice.issued_at', false);
        $this->dateOrNull($invoice['due_at'], 'invoice.due_at', true);
        $this->dateOrNull($invoice['paid_at'], 'invoice.paid_at', true);
        $this->string($invoice['status'], 'invoice.status', 32, '/^[a-z][a-z_]*$/D');
        $this->string($invoice['currency'], 'invoice.currency', 3, '/^[A-Z]{3}$/D');

        $issuer = $this->object($snapshot['issuer'], 'issuer');
        $this->keys($issuer, ['company_name', 'address', 'website', 'support_email', 'footer_email'], 'issuer');
        $this->required($issuer, ['company_name', 'address', 'website', 'support_email', 'footer_email'], 'issuer');
        $this->plainText($issuer['company_name'], 'issuer.company_name', 191);
        $this->plainTexts($issuer['address'], 'issuer.address', 8, 191);
        if (!is_string($issuer['website']) || !in_array($issuer['website'], ['', 'ppflight.com', 'www.ppflight.com'], true)) {
            throw new RenderException('issuer.website must be a permitted PPFlight display domain or empty.');
        }
        $this->email($issuer['support_email'], 'issuer.support_email');
        $this->email($issuer['footer_email'], 'issuer.footer_email');

        $customer = $this->object($snapshot['customer'], 'customer');
        $this->keys($customer, ['company', 'name', 'email', 'address'], 'customer');
        $this->required($customer, ['company', 'name', 'email', 'address'], 'customer');
        $this->stringOrNull($customer['company'], 'customer.company', 160);
        $this->string($customer['name'], 'customer.name', 160);
        $this->email($customer['email'], 'customer.email');
        $this->strings($customer['address'], 'customer.address', 8, 160);

        $this->items($snapshot['line_items']);
        $this->payments($snapshot['payments']);
        $amounts = $this->object($snapshot['amounts'], 'amounts');
        $amountKeys = ['subtotal', 'discount', 'tax', 'total', 'amount_paid', 'balance_due'];
        $this->keys($amounts, $amountKeys, 'amounts');
        $this->required($amounts, $amountKeys, 'amounts');
        foreach ($amountKeys as $key) {
            $this->amount($amounts[$key], 'amounts.' . $key);
        }

        return $snapshot;
    }

    /** @param mixed $value */
    private function object($value, string $path): array
    {
        if (!is_array($value) || array_is_list($value)) {
            throw new RenderException($path . ' must be an object.');
        }
        return $value;
    }

    /** @param array<string,mixed> $actual @param list<string> $allowed */
    private function keys(array $actual, array $allowed, string $path): void
    {
        foreach (array_keys($actual) as $key) {
            if (!is_string($key) || !in_array($key, $allowed, true)) {
                throw new RenderException('Unknown field in ' . $path . '.');
            }
        }
    }

    /** @param array<string,mixed> $actual @param list<string> $required */
    private function required(array $actual, array $required, string $path): void
    {
        foreach ($required as $key) {
            if (!array_key_exists($key, $actual)) {
                throw new RenderException('Missing ' . $path . '.' . $key . '.');
            }
        }
    }

    /** @param mixed $value */
    private function string($value, string $path, int $max, ?string $pattern = null): void
    {
        // Protocol limits are Unicode character limits. Counting UTF-8 bytes
        // would reject ordinary Chinese issuer/customer text long before the
        // documented field limit.
        if (!is_string($value) || $value === '' || mb_strlen($value, 'UTF-8') > $max || ($pattern !== null && preg_match($pattern, $value) !== 1)) {
            throw new RenderException($path . ' is invalid.');
        }
    }

    /** @param mixed $value */
    private function stringOrNull($value, string $path, int $max): void
    {
        if ($value !== null) {
            $this->string($value, $path, $max);
        }
    }

    /** Issuer legal copy must be ordinary text, never a mini-markup or link field. */
    private function plainText($value, string $path, int $max): void
    {
        $this->string($value, $path, $max);
        if (preg_match('/[<>\x00-\x1F\x7F]/u', $value) === 1 || preg_match('/(?:https?:\/\/|file:\/\/|www\.)/iu', $value) === 1) {
            throw new RenderException($path . ' must be plain text.');
        }
    }

    /** @param mixed $value */
    private function email($value, string $path): void
    {
        $this->string($value, $path, 254);
        if (filter_var($value, FILTER_VALIDATE_EMAIL) === false) {
            throw new RenderException($path . ' is invalid.');
        }
    }

    /** @param mixed $value */
    private function dateOrNull($value, string $path, bool $nullable): void
    {
        if ($nullable && $value === null) {
            return;
        }
        $this->string($value, $path, 10, '/^\d{4}-\d{2}-\d{2}$/D');
        $date = \DateTimeImmutable::createFromFormat('!Y-m-d', $value);
        if ($date === false || $date->format('Y-m-d') !== $value) {
            throw new RenderException($path . ' must be an ISO date.');
        }
    }

    /** @param mixed $value */
    private function amount($value, string $path): void
    {
        $this->string($value, $path, 16, '/^-?[0-9]{1,12}(\.[0-9]{1,2})?$/D');
    }

    /** @param mixed $value */
    private function strings($value, string $path, int $maxItems, int $maxLength): void
    {
        if (!is_array($value) || !array_is_list($value) || count($value) > $maxItems) {
            throw new RenderException($path . ' is invalid.');
        }
        foreach ($value as $index => $string) {
            $this->string($string, $path . '[' . $index . ']', $maxLength);
        }
    }

    /** @param mixed $value */
    private function plainTexts($value, string $path, int $maxItems, int $maxLength): void
    {
        if (!is_array($value) || !array_is_list($value) || count($value) > $maxItems) {
            throw new RenderException($path . ' is invalid.');
        }
        foreach ($value as $index => $string) {
            $this->plainText($string, $path . '[' . $index . ']', $maxLength);
        }
    }

    /** @param mixed $value */
    private function items($value): void
    {
        if (!is_array($value) || !array_is_list($value) || count($value) > self::MAX_LINE_ITEMS) {
            throw new RenderException('line_items is invalid.');
        }
        foreach ($value as $index => $item) {
            $item = $this->object($item, 'line_items[' . $index . ']');
            $keys = ['description', 'quantity', 'unit_amount', 'total'];
            $this->keys($item, $keys, 'line_items[' . $index . ']');
            $this->required($item, $keys, 'line_items[' . $index . ']');
            $this->string($item['description'], 'line_items[' . $index . '].description', 512);
            if (!is_int($item['quantity']) || $item['quantity'] < 0 || $item['quantity'] > 1000000) {
                throw new RenderException('line_items[' . $index . '].quantity is invalid.');
            }
            $this->amount($item['unit_amount'], 'line_items[' . $index . '].unit_amount');
            $this->amount($item['total'], 'line_items[' . $index . '].total');
        }
    }

    /** @param mixed $value */
    private function payments($value): void
    {
        if (!is_array($value) || !array_is_list($value) || count($value) > self::MAX_PAYMENTS) {
            throw new RenderException('payments is invalid.');
        }
        foreach ($value as $index => $payment) {
            $payment = $this->object($payment, 'payments[' . $index . ']');
            $keys = ['provider', 'reference', 'amount', 'paid_at'];
            $this->keys($payment, $keys, 'payments[' . $index . ']');
            $this->required($payment, $keys, 'payments[' . $index . ']');
            $this->string($payment['provider'], 'payments[' . $index . '].provider', 80);
            $this->string($payment['reference'], 'payments[' . $index . '].reference', 160);
            $this->amount($payment['amount'], 'payments[' . $index . '].amount');
            $this->dateOrNull($payment['paid_at'], 'payments[' . $index . '].paid_at', false);
        }
    }
}

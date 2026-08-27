<?php

declare(strict_types=1);

namespace PPFlight\InvoiceRenderer;

final class RenderException extends \RuntimeException
{
    private const DIAGNOSTIC_CODES = [
        'artifact',
        'cache',
        'dependencies',
        'input',
        'render',
    ];

    public function __construct(
        string $message,
        private readonly string $diagnosticCode = 'input',
        ?\Throwable $previous = null,
    ) {
        if (!in_array($diagnosticCode, self::DIAGNOSTIC_CODES, true)) {
            throw new \InvalidArgumentException('Invalid renderer diagnostic code.');
        }
        parent::__construct($message, 0, $previous);
    }

    public function diagnosticCode(): string
    {
        return $this->diagnosticCode;
    }
}

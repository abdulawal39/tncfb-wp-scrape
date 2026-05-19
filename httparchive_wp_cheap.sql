-- Cheap variant: scans only the `technologies` column (~5-20 GB), not `payload`.
-- Expected cost: < $0.10 per run, fits in BigQuery free tier easily.
-- Tradeoff: no PDF/catalog signal at SQL stage — recover it via enrich.py.
--
-- Replace @DATE with latest crawl date, e.g. 2026-04-01.

SELECT
  NET.REG_DOMAIN(page) AS domain,
  page AS url,
  JSON_VALUE(summary, '$.rank') AS crux_rank,
  ARRAY_TO_STRING(
    ARRAY(SELECT t.technology FROM UNNEST(technologies) t
          WHERE t.technology IN (
            'WordPress','WooCommerce','Elementor','Issuu','FlipHTML5',
            'Yoast SEO','WPBakery','Divi','Avada'
          )),
    '|'
  ) AS tech_signals,
  -- Boolean flags for fast filtering downstream:
  EXISTS(SELECT 1 FROM UNNEST(technologies) t WHERE t.technology = 'Issuu') AS uses_issuu,
  EXISTS(SELECT 1 FROM UNNEST(technologies) t WHERE t.technology = 'FlipHTML5') AS uses_fliphtml5,
  EXISTS(SELECT 1 FROM UNNEST(technologies) t WHERE t.technology = 'WooCommerce') AS uses_woo
FROM `httparchive.crawl.pages`
WHERE date = DATE('@DATE')
  AND client = 'desktop'
  AND is_root_page = TRUE
  AND EXISTS (
    SELECT 1 FROM UNNEST(technologies) t
    WHERE t.technology = 'WordPress'
  )
ORDER BY SAFE_CAST(JSON_VALUE(summary, '$.rank') AS INT64) NULLS LAST
LIMIT 100000;

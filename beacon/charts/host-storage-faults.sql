-- title: Storage Faults
-- resource_type: Metal Host
-- section: Disk
-- unit:
-- style: Line
-- is_key_chart: 0
-- sort_order: 60
-- The fault counts that the host-storage-failed and host-hardware-degraded rules read. Each is a total, so a step up is a new fault.
-- ZFS shows the device with the most errors of each kind and pool. A critical warning counts the NVMe drives that report one.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, max(total) AS value
FROM (
    SELECT s.ts AS sample_ts,
           multiIf(s.metric = 'system_md_degraded', 'Missing RAID disks', s.metric = 'system_zfs_device_errors_maximum', 'ZFS device errors',
                   s.metric = 'system_zfs_device_slow_ios', 'ZFS slow I/Os', s.labels['field'] = 'media_errors', 'NVMe media errors', 'NVMe critical warnings') AS series,
           if(series = 'ZFS slow I/Os', max(s.value), sum(if(series = 'NVMe critical warnings', s.value > 0, s.value))) AS total
    FROM datum.samples AS s
    WHERE (s.metric IN ('system_md_degraded', 'system_zfs_device_errors_maximum', 'system_zfs_device_slow_ios')
            OR (s.metric = 'system_nvme_smart' AND s.labels['field'] IN ('media_errors', 'critical_warning')))
        AND s.resource_id = {resource_id:String} AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts, series
)
GROUP BY ts, series
ORDER BY ts, series

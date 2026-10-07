SELECT w.*
FROM (
    SELECT
        j.*,
        ISNULL(u.customercode, '') AS rep_customercode,
        q.QTSKUNO,
        q.QQuotationNo,
        q.QGwt,
        q.QNwt,
        q.QPwt,
        q.QDwt,
        q.QDpcs,
        q.QCSwt,
        q.QCSpcs,
        q.QMiscwt,
        q.QMiscpcs,
        q.QMetalAmt,
        q.QDiaAmt,
        q.QCsAmt,
        q.QMiscAmt,
        q.QLOAmt,
        q.QFinalAmt,
        SUM(ISNULL(j.Job_Quantity, 0)) OVER (
            PARTITION BY j.SKUNO, j.po, j.IsSampleLineJob, j.systemloginmaster_customerid,
                j.Job_customercode, j.Job_customerfirmname, j.jobdate, j.isdesignsetjob,
                j.designsetnolist, j.Quotation_SKUNo, j.loginUserCode, j.IsSignature,
                j.Prefix, j.Postfix, j.UNOPP, j.OrderTypeId, j.JobBulkNo,
                ISNULL(j.SalesrepCode, ''), ISNULL(u.customercode, ''),
                j.orders, j.orderType, j.SoldPending
        ) AS grp_total_qty,
        SUM(CASE WHEN j.jobflowfrom NOT IN ('advfgpurchaseneworder', 'advfgpurchasereorder') THEN 1 ELSE 0 END) OVER (
            PARTITION BY j.SKUNO, j.po, j.IsSampleLineJob, j.systemloginmaster_customerid,
                j.Job_customercode, j.Job_customerfirmname, j.jobdate, j.isdesignsetjob,
                j.designsetnolist, j.Quotation_SKUNo, j.loginUserCode, j.IsSignature,
                j.Prefix, j.Postfix, j.UNOPP, j.OrderTypeId, j.JobBulkNo,
                ISNULL(j.SalesrepCode, ''), ISNULL(u.customercode, ''),
                j.orders, j.orderType, j.SoldPending
        ) AS grp_non_reorder
    FROM (
        SELECT
            Job.SKUNO,
            Job.JobNo,
            Job.po,
            Job.systemloginmaster_customerid,
            Job.Job_customercode,
            Job.Job_customerfirmname,
            Job.jobdate,
            Job.Designcode,
            Job.jobflowfrom,
            Job.issupplierissue,
            Job.isdesignsetjob,
            Job.designsetnolist,
            Job.JobManagement_ProgressStatusMasterid,
            Job.Job_Quantity,
            Job.job_RemainingQuantity,
            Job.IsCustomizeJob,
            Job.isdelete,
            Job.systemloginmaster_salesrepid,
            Job.IsSampleLineJob,
            ISNULL(Job.Quotation_SKUNo, '') AS Quotation_SKUNo,
            ISNULL(Job.QuotationNo, '') AS QuotationNo,
            ISNULL(Job.loginUserCode, '') AS loginUserCode,
            Job.IsSignature,
            Job.Prefix,
            Job.Postfix,
            Job.UNOPP,
            Job.OrderTypeId,
            Job.JobBulkNo,
            Job.IsBulkjob,
            ISNULL(Job.SalesrepCode, '') AS SalesrepCode,
            Job.SalesrepId,
            Job.Job_salesrepusercode,
            CASE
                WHEN ISNULL(Job.IsSampleLineJob, 0) = 0 AND ISNULL(Job.jobflowfrom, '') <> 'ReceiveToRepair' THEN 'Regular jobs'
                WHEN ISNULL(Job.IsSampleLineJob, 0) = 1 AND ISNULL(Job.jobflowfrom, '') <> 'ReceiveToRepair' THEN 'Sample line jobs'
                ELSE ''
            END AS orders,
            CASE
                WHEN ISNULL(Job.OrderTypeId, 0) = 1 THEN 'Regular'
                WHEN ISNULL(Job.OrderTypeId, 0) = 2 THEN 'Corporate'
                ELSE ''
            END AS orderType,
            CASE
                WHEN ISNULL(Job.Job_Quantity, 0) > 0 AND ISNULL(Job.job_RemainingQuantity, 0) = 0 THEN 'sold'
                WHEN ISNULL(Job.Job_Quantity, 0) > 0 AND ISNULL(Job.job_RemainingQuantity, 0) <> 0 THEN 'pending'
                ELSE ''
            END AS SoldPending
        FROM (
            SELECT SKUNO, po, systemloginmaster_customerid, Job_customercode, Job_customerfirmname,
                   jobdate, Designcode, jobflowfrom, issupplierissue, isdesignsetjob,
                   designsetnolist, JobManagement_ProgressStatusMasterid, Job_Quantity,
                   job_RemainingQuantity, IsCustomizeJob, isdelete, systemloginmaster_salesrepid,
                   IsSampleLineJob, Quotation_SKUNo, QuotationNo, loginUserCode, JobNo,
                   IsSignature, Prefix, Postfix, UNOPP, OrderTypeId,
                   IIF(JobBulkNo > 0, 1, 0) AS JobBulkNo, IsBulkjob,
                   CASE WHEN ISNULL(SalesrepId, 0) = 0 THEN Job_salesrepusercode ELSE SalesrepCode END AS SalesrepCode,
                   SalesrepId, Job_salesrepusercode
            FROM [dbo].JobManagement_JobMaster WITH (NOLOCK)
            WHERE ISNULL(IsCustomizeJob, 0) = 0
              AND ISNULL(IsBulkjob, 0) = 0
              AND ISNULL(jobflowfrom, '') <> 'Retail'
              AND ISNULL(SKUNO, '') <> ''
              AND ISNULL(isdelete, 0) = 0
              AND jobflowfrom NOT IN ('advfgpurchaseneworder', 'ReceiveToRepair')
            UNION ALL
            SELECT SKUNO, po, systemloginmaster_customerid, Job_customercode, Job_customerfirmname,
                   jobdate, Designcode, jobflowfrom, issupplierissue, isdesignsetjob,
                   designsetnolist, JobManagement_ProgressStatusMasterid, Job_Quantity,
                   job_RemainingQuantity, IsCustomizeJob, isdelete, systemloginmaster_salesrepid,
                   IsSampleLineJob, Quotation_SKUNo, QuotationNo, loginUserCode, JobNo,
                   IsSignature, Prefix, Postfix, UNOPP, OrderTypeId,
                   IIF(JobBulkNo > 0, 1, 0) AS JobBulkNo, IsBulkjob,
                   CASE WHEN ISNULL(SalesrepId, 0) = 0 THEN Job_salesrepusercode ELSE SalesrepCode END AS SalesrepCode,
                   SalesrepId, Job_salesrepusercode
            FROM [dbo].JobManagement_JobMaster_closed WITH (NOLOCK)
            WHERE ISNULL(IsCustomizeJob, 0) = 0
              AND ISNULL(IsBulkjob, 0) = 0
              AND ISNULL(jobflowfrom, '') <> 'Retail'
              AND ISNULL(SKUNO, '') <> ''
              AND ISNULL(isdelete, 0) = 0
              AND IsQuotationJobBillArchived = 3
              AND jobflowfrom NOT IN ('advfgpurchaseneworder', 'ReceiveToRepair')
        ) Job
    ) j
    LEFT JOIN (
        SELECT
            a.job_JobNo,
            a.SKUNo AS QTSKUNO,
            a.QuotationNo AS QQuotationNo,
            ISNULL(a.GrossCTWWithLoss, 0) * ISNULL(a.Quantity, 0) AS QGwt,
            ISNULL(a.Netwt, 0) * ISNULL(a.Quantity, 0) AS QNwt,
            CONVERT(DECIMAL(38, 3),
                CASE
                    WHEN ISNULL(mp_both.price_ratio, 0) > 0 AND ISNULL(mp_to.price_ratio, 0) > 0
                        THEN ISNULL(a.Netwt, 0) * ISNULL(a.Quantity, 0) * mp_both.price_ratio / mp_to.price_ratio
                    ELSE ISNULL(a.Netwt, 0) * ISNULL(a.Quantity, 0) * ISNULL(mp_concat.price_ratio, 0) / 100
                END
            ) AS QPwt,
            ISNULL(a.DiamondCTWwithLoss, 0) * ISNULL(a.Quantity, 0) AS QDwt,
            ISNULL(a.diamond_totalpieces, 0) * ISNULL(a.Quantity, 0) AS QDpcs,
            ISNULL(a.ActualColorStoneWeight, 0) * ISNULL(a.Quantity, 0) AS QCSwt,
            ISNULL(a.colorstone_totalpieces, 0) * ISNULL(a.Quantity, 0) AS QCSpcs,
            ISNULL(a.totalmiscweight, 0) * ISNULL(a.Quantity, 0) AS QMiscwt,
            ISNULL(a.totalmiscpcs, 0) * ISNULL(a.Quantity, 0) AS QMiscpcs,
            CONVERT(decimal(38, 2), ISNULL(a.MetalAmount, 0) * ISNULL(a.Quantity, 0)) AS QMetalAmt,
            CONVERT(decimal(38, 2), ISNULL(a.DiamondAmount, 0) * ISNULL(a.Quantity, 0)) AS QDiaAmt,
            CONVERT(decimal(38, 2), ISNULL(a.CsAmount, 0) * ISNULL(a.Quantity, 0)) AS QCsAmt,
            CONVERT(decimal(38, 2), ISNULL(a.MiscAmount, 0) * ISNULL(a.Quantity, 0)) AS QMiscAmt,
            CONVERT(decimal(38, 2), ISNULL(a.LabourAmount, 0)) AS QLOAmt,
            ISNULL(a.FinalAmount, 0) AS QFinalAmt
        FROM (
            SELECT SKUNo, QuotationNo, GrossCTWWithLoss, Quantity, Netwt, MasterManagement_goldtypename,
                   DiamondCTWwithLoss, diamond_totalpieces, ActualColorStoneWeight, colorstone_totalpieces,
                   FinalAmount, MasterManagement_goldtypeid, Totalmiscappliedweight, totalmiscweight, totalmiscpcs,
                   ISNULL(TotalMetalCost, 0) + ISNULL(MM_TotalMetalCost, 0) + ISNULL(TotalFindingCost, 0) AS MetalAmount,
                   TotalDiaLGCost AS DiamondAmount,
                   TotalColorStoneCost AS CsAmount,
                   TotalMiscCost AS MiscAmount,
                   FinalAmount - ((ISNULL(TotalMetalCost, 0) + ISNULL(MM_TotalMetalCost, 0) + ISNULL(TotalFindingCost, 0) + ISNULL(TotalDiaLGCost, 0) + ISNULL(TotalColorStoneCost, 0) + ISNULL(TotalMiscCost, 0)) * [Quantity]) AS LabourAmount,
                   job_JobNo
            FROM [dbo].Quotationmanagement_dcbdesignInfo WITH (NOLOCK)
            WHERE job_JobNo > 0
            UNION ALL
            SELECT SKUNo, QuotationNo, GrossCTWWithLoss, Quantity, Netwt, MasterManagement_goldtypename,
                   DiamondCTWwithLoss, diamond_totalpieces, ActualColorStoneWeight, colorstone_totalpieces,
                   FinalAmount, MasterManagement_goldtypeid, Totalmiscappliedweight, totalmiscweight, totalmiscpcs,
                   ISNULL(TotalMetalCost, 0) + ISNULL(MM_TotalMetalCost, 0) + ISNULL(TotalFindingCost, 0) AS MetalAmount,
                   TotalDiaLGCost AS DiamondAmount,
                   TotalColorStoneCost AS CsAmount,
                   TotalMiscCost AS MiscAmount,
                   FinalAmount - ((ISNULL(TotalMetalCost, 0) + ISNULL(MM_TotalMetalCost, 0) + ISNULL(TotalFindingCost, 0) + ISNULL(TotalDiaLGCost, 0) + ISNULL(TotalColorStoneCost, 0) + ISNULL(TotalMiscCost, 0)) * [Quantity]) AS LabourAmount,
                   job_JobNo
            FROM [dbo].Quotationmanagement_dcbdesignInfo_Archive WITH (NOLOCK)
            WHERE job_JobNo > 0
              AND IsQuotationJobBillArchived = 3
        ) a
        LEFT JOIN [dbo].Mastermanagement_metaltype b WITH (NOLOCK)
            ON a.MasterManagement_goldtypeid = b.autocode
        LEFT JOIN [dbo].Mastermanagement_MetalPurity mp_to WITH (NOLOCK)
            ON b.metaltypename = mp_to.metaltypename AND mp_to.IsBaseMetal = 1
        LEFT JOIN [dbo].Mastermanagement_MetalPurity mp_both WITH (NOLOCK)
            ON b.metaltypename = mp_both.metaltypename AND b.metalPurity = mp_both.metalPurity
        LEFT JOIN [dbo].Mastermanagement_MetalPurity mp_concat WITH (NOLOCK)
            ON CONCAT(mp_concat.metaltypename, ' ', mp_concat.metalPurity) = b.metalPurity
    ) q ON j.JobNo = q.job_JobNo
    LEFT JOIN [dbo].Usermanagement_systemloginmaster u WITH (NOLOCK)
        ON u.id = j.systemloginmaster_salesrepid
) w
WHERE w.grp_total_qty > 0 AND w.grp_non_reorder > 0

SELECT
    dcb.*,
    ISNULL(cust.CustomerName, '') AS TaxCustomerName,
    ISNULL(cust.CustomerFullName, '') AS TaxCustomerFullName,
    ISNULL(grp.CustomerGroup, '') AS TaxCustomerGroup
FROM [dbo].Stockmanagement_dcb AS dcb
OUTER APPLY (
    SELECT TOP 1
        h.usermanagement_customercode AS CustomerName,
        LTRIM(RTRIM(ISNULL(h.usermanagement_customerfirstname, '') + ' '
            + ISNULL(h.usermanagement_customerlastname, ''))) AS CustomerFullName,
        h.usermanagement_customerid AS CustomerId
    FROM [dbo].Stockmanagement_dcbdesignInfo_history AS h WITH (NOLOCK)
    WHERE h.StockDocumentNo = dcb.StockDocumentNo
    ORDER BY h.id
) AS cust
OUTER APPLY (
    SELECT TOP 1
        ISNULL(u.MasterManagement_BusinessClassname, '') AS CustomerGroup
    FROM [dbo].Usermanagement_systemloginmaster AS u WITH (NOLOCK)
    WHERE u.id = cust.CustomerId
) AS grp

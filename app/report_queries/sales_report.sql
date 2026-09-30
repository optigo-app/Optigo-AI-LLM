
SELECT a.*,
       a.MasterManagement_BusinessClassname AS CustomerType,
       a.IsSampleLineJob AS jobtype,
       a.GroupJob AS IsClub,
       a.LabourAmount AS totalLabourAmt,
       a.OtherAmount AS totalOtherAmt,
       a.usermanagement_salesrepcode AS SalesRep,
       a.D_F_Pcs_Cm AS Co_DiaPCS,
       a.D_F_Wt_Cm AS Co_DiaWt,
       a.D_F_Pcs_Ct AS Cu_DiaPCS,
       a.D_F_Wt_Ct AS Cu_DiaWt
FROM
(
				Select  
					DI.entrydate as [EntryDate]
					,DI.id
					,DI.entrydate as date
					,[DI].[StockBarcode]
					,[DI].[designno]
					,[DI].[uniqueno]
					,[DI].StockDocumentNo as Stockdocumentno																																												
					,isnull(DI.usermanagement_customercode,'') as CustomerName
					,concat(DI.usermanagement_customerfirstname,' ',DI.usermanagement_customerlastname) as CustomerFullName
					,isnull(DI.mastermanagement_categoryname,'') as categoryname
					,isnull(DI.mastermanagement_subcategoryname,'') as subcategoryname							
					,isnull(DI.MasterManagement_goldcolorname,'') as goldcolorname									
					,DI.Stockmanagement_Statusid
					,'' as statusname							
					,isnull(Di.StockBarcode,'') as jobno
					,replace(isnull(DI.StockBarcode,''),'_','') as jobno_excel
					,isnull(Di.job_skuno,'') as SKUNO
					,isnull(Di.autocode,'') as autocode
					,isnull(Di.design_DefaultImageName,'') as DefaultImageName
					,isnull(Di.imgrandomno,'') as imgrandomno
					,isnull(DI.mastermanagement_goldtypename,'') as goldtypename							
					,isnull(DI.IsERPreturn,0) as IsERPreturn
					,convert(decimal(38,2),isnull(DI.design_TotalAmouont,0)) as Amount
					,convert(decimal(38,2),isnull(DI.design_TotalAmouont,0)) as design_TotalAmouont
					,isnull(DI.GrossCTWWithLoss,0) as grosswt
					,convert(decimal(38,3),((isnull(DI.MetalDiamondWeightWithLoss,0)-isnull(DI.FindingWtWithLoss,0))*(ISNULL(DI.wastage,0)+ISNULL(DI.metalPriceRatio,0))/100))+isnull(DI.calculatemetalwtloss,0)+isnull(b.PureWt,0)+isnull(DI.PureFindingWt,0) as netwt_24k
					,isnull(DI.diamond_totalpieces,0) as dpcs
					,isnull(DI.DiamondCTWwithLoss,0) as dctw
					,isnull(di.colorstone_totalpieces,0) as cspcs
					,isnull(DI.ActualColorStoneWeight,0) as csctw
					,isnull(DI.totalmiscpcs,0) as miscpcs
					,isnull(DI.Totalmiscappliedweight,0) as miscwt
					,case when isnull(DI.jobflowfrom,'')='MFGJob' then 0 else 1 end as IsSupplierJob
					,isnull(Di.IsJobClosed,0) as IsJobClosed
					,convert(decimal(38,2),isnull(TotalMetalAmount,0)+isnull(TotalFindingAmount,0)+isnull(MM_TotalMetalAmount,0)) as MetalAmount
					,convert(decimal(38,2),isnull(DI.TotalDiamondCost,0)) as DiamondAmount 
					,convert(decimal(38,2),isnull(DI.TotalColorStoneCost,0)) as ColorStoneAmount
					,convert(decimal(38,2),isnull(TotalMakingAmount,0))
						+convert(decimal(38,2),isnull(totaldiasettingcost,0))
						+convert(decimal(38,2),isnull(totalCSsettingcost,0))
						+convert(decimal(38,2),isnull(TotalFindingSettingCost,0))
						+convert(decimal(38,2),isnull(MM_TotalMakingAmount,0)) as LabourAmount

			,convert(decimal(38,2),isnull(TotalPriorityChargesAmount,0)+isnull(DI.TotalMiscCost,0)+convert(decimal(38,3),isnull(DI.TotalWastageAmount,0))+isnull(DI.OtherCharges,0)+(isnull(TotalDiamondHandling,0)))  as OtherAmount
			,convert(decimal(38,2),isnull(DI.UnitCost,0)) as UnitCost
			,isnull(DI.Mastermanagement_FG_StockLockername,'') as Mastermanagement_FG_StockLockername
			,0 as Isarchive
			,isnull(DI.Stockmanagement_QCStatusid,0) as QCStatusid
			,isnull(DI.Stockmanagement_QCStatusname,'') as QCStatusname 
			,(CASE(isWithHallMark) WHEN 1 THEN 'With HallMark' WHEN 2 THEN 'Without HallMark' 
								ELSE '' END )  as isWithHallMark
			,isnull(DI.MasterManagement_producttypeid,0) as producttypeid
			,isnull(DI.MasterManagement_producttypename,'') as producttypename
			,isnull(DI.[IsSampleLineJob],0) as IsSampleLineJob
			,convert(decimal(38,2),isnull(case when isnull(isdiscountinamount,0)=1
								then isnull(Discount,0)
								else case when isnull(IsCriteriabasedAmount,0)=1
									then isnull(DiamondDiscountAmount,0)
										+ isnull(StoneDiscountAmount,0)
										+ isnull(MetalDiscountAmount,0)
										+ isnull(LabourDiscountAmount,0)
										+ isnull(SolitaireDiscountAmount,0)
										+ isnull(MiscDiscountAmount,0)
									else convert(decimal(38,3),((isnull(UnitCost,0)*isnull(Discount,0))/100.000))
									end
								end,0) + isnull(Solitaire_Discount,0))
					as Discount
			,isnull(DI.mastermanagement_collectionname,'') as collection
			,isnull(DI.usermanagement_customerid,0) as customerid
			,isnull(dcb.mastermanagement_customizeprintname,'') as customizeprintname
			,isnull(dcb.PrintUnique,'') as PrintUnique
			,isnull(dcb.totaltaxAmount,0) as totaltaxAmount
			,replace(isnull(DI.GroupJob,''),'_','') as GroupJob
			,convert(decimal(38,3),isnull(netweight,0)) as netwt
			,ISNULL([MetalRate24K],0) AS MetalRate24K
			,'' as IsshowCustomerName
			,ISNULL(di.TotalSettingCost,0) as TotalSettingCost
			,ISNULL(di.TotalDiamondHandling,0) as TotalDiamondHandling
			,0 as D_Pcs_Cm
			,0 as D_Wt_Cm
			,0 as D_Pcs_Ct
			,0 as D_Wt_Ct
			,0 as C_Pcs_Cm
			,0 as C_Wt_Cm
			,0 as C_Pcs_Ct
			,0 as C_Wt_Ct
			,dcb.Prefix
			,dcb.Postfix
			,dcb.billnoPrefixPostfix
			,dcb.IsReactPrint
			,ISNULL(DI.metalPriceRatio,0) as Tunch
			,ISNULL(DI.wastage,0) as Wastage
			,isnull(Metal_Type_Name,'') as Metal_Type_Name
			,isnull(MetalDiamondWeightWithLoss,0)+isnull(MM_MetalWeightWithLoss,0)-isnull(NetWeight,0) as MetalLoss
			,isnull(MetalDiamondWeightWithLoss,0)+isnull(MM_MetalWeightWithLoss,0) as NetWtWithLoss 
			,J_Jobno
			,isnull(ArticleMasterCode,'') as ArticleMasterCode
			,taxfilter
			,billmode
			,orderform
			,Manufacturer
			,mastermanagement_brandname
			,US.MasterManagement_BusinessClassname	as MasterManagement_BusinessClassname
			,DI.usermanagement_salesrepcode			as usermanagement_salesrepcode 
			,ISNULL(BB.packageWt,0)					as packageWt
			,convert(decimal(38,3),((isnull(DI.MetalDiamondWeightWithLoss,0)-isnull(DI.FindingWtWithLoss,0))*(ISNULL(DI.wastage,0))/100))+isnull(b.MM_Wastage,0) as Wastage_24k
			,iif(Metal_Type_Name='gold',MWt,0)+isnull(Gold_Wt,0) as GoldWt
			,iif(Metal_Type_Name='silver',MWt,0)+isnull(Silver_Wt,0) as SilverWt
			,iif(Metal_Type_Name='platinum',MWt,0)+isnull(Platinum_Wt,0) as PlatinumWt
			,iif(Metal_Type_Name not in ('gold','silver','platinum'),MWt,0)+isnull(Other_Wt,0) as OtherWt

			,convert(decimal(38,2),iif(Metal_Type_Name='gold',TotalMetalAmount,0)+isnull(Gold_Amt,0)) as GoldAmt
			,convert(decimal(38,2),iif(Metal_Type_Name='silver',TotalMetalAmount,0)+isnull(Silver_Amt,0)) as SilverAmt
			,convert(decimal(38,2),iif(Metal_Type_Name='platinum',TotalMetalAmount,0)+isnull(Platinum_Amt,0)) as PlatinumAmt
			,convert(decimal(38,2),iif(Metal_Type_Name not in ('gold','silver','platinum'),TotalMetalAmount,0)+isnull(Other_Amt,0)) as OtherAmt
			,MM_Wastage_Gold_Wt,MM_Wastage_Silver_Wt,MM_Wastage_Platinum_Wt,MM_Wastage_Other_Wt
			,Pure_Gold_Wt,Pure_Silver_Wt,Pure_Platinum_Wt,Pure_Other_Wt
			,jewellerysize
			,convert(decimal(38,2),iif(isnull(metalPriceRatio,0)+isnull(wastage,0)=0
							,0
							,((isnull(MetalDiamondWeightWithLoss,0)-isnull(FindingWtWithLoss,0))*isnull(MetalRate,0))-convert(decimal(38,3),(isnull(MetalDiamondWeightWithLoss,0)-isnull(FindingWtWithLoss,0))*(isnull(MetalRate,0)*(isnull(metalPriceRatio,0)/(isnull(metalPriceRatio,0)+isnull(wastage,0)))))
						)+isnull(MM_WastageAmount,0)+isnull(FindingWastageAmount,0)) as WastageAmount
			,VersionName
			,ISNULL(bn.D_F_Pcs_Cm, 0) AS D_F_Pcs_Cm
			,ISNULL(bn.D_F_Wt_Cm, 0) AS D_F_Wt_Cm
			,ISNULL(bn.D_F_Pcs_Ct, 0) AS D_F_Pcs_Ct
			,ISNULL(bn.D_F_Wt_Ct, 0) AS D_F_Wt_Ct
		from
			(
				select id,StockDocumentNo,EntryDate,StockBarcode,designno,uniqueno,usermanagement_customercode,usermanagement_customerfirstname
					,mastermanagement_categoryname,usermanagement_customerlastname,mastermanagement_subcategoryname,MasterManagement_goldcolorname
					,Stockmanagement_Statusid,Job_SKUNo,autocode,design_defaultimagename,imgrandomno,mastermanagement_goldtypename,IsERPReturn
					,design_TotalAmouont,GrossCTWWithLoss,MetalDiamondWeightWithLoss,FindingWtWithLoss,wastage,metalPriceRatio,calculatemetalwtloss
					,PureFindingWt,diamond_totalpieces,DiamondCTWwithLoss,colorstone_totalpieces,ActualColorStoneWeight,totalmiscpcs
					,Totalmiscappliedweight,jobflowfrom,IsJobClosed,TotalMetalAmount,MM_TotalMetalAmount,TotalDiamondCost,TotalColorStoneCost
					,TotalMakingAmount,TotalDiaSettingCost,TotalCSSettingCost,TotalFindingSettingCost,TotalPriorityChargesAmount
					,TotalMiscCost,TotalWastageAmount,OtherCharges,TotalDiamondHandling,UnitCost,Mastermanagement_FG_StockLockername
					,Stockmanagement_QCStatusid,Stockmanagement_QCStatusname,isWithHallMark,MasterManagement_producttypeid
					,MasterManagement_producttypename,IsSampleLineJob,isdiscountinamount,Discount,IsCriteriabasedAmount,DiamondDiscountAmount
					,MetalDiscountAmount,StoneDiscountAmount,LabourDiscountAmount,SolitaireDiscountAmount,MiscDiscountAmount
					,mastermanagement_collectionname,usermanagement_customerid,GroupJob,NetWeight,TotalSettingCost,Metal_Type_Name
					,MM_MetalWeightWithLoss,J_JobNo,MM_TotalMakingAmount,ArticleMasterCode
					,case when (isnull(b2c_orderno,'')='' and isnull(b2c_orderno_stock,'')='')  then 'offline order'
						when (isnull(b2c_orderno,'')<>'' or isnull(b2c_orderno_stock,'')<>'')  then 'online order'
					end as orderform
					,CASE WHEN isnull(DI.Stock_Contractorcode,'') <> '' THEN isnull(DI.Stock_Contractorcode,'')
						ELSE  isnull(DI.Stock_Suppliercode,'') END AS Manufacturer
					,mastermanagement_brandname
					,usermanagement_salesrepcode
					,MetalRate,convert(decimal(38,3),isnull(netweight,0)-isnull(MM_MetalWeightWithLoss,0)-isnull(FindingWtWithLoss,0)) as MWt
					,TotalFindingAmount
					,convert(decimal(38,3),((isnull(DI.MetalDiamondWeightWithLoss,0)+isnull(DI.FindingWtWithLoss,0))
							*(ISNULL(DI.wastage,0)+ISNULL(DI.metalPriceRatio,0))/100))
							+isnull(DI.calculatemetalwtloss,0)					
							as Pure_Gold_Wt
					,jewellerysize
					,ISNULL(J_Version,'') as VersionName
				FROM [dbo].[Stockmanagement_dcbdesignInfo_history]  as DI with (nolock)
				where Stockmanagement_Statusid=16
				and 1=1 
				and isnull([DI].StockDocumentNo,'')<>''
			) as DI
		inner join (
							select * 
							FROM (SELECT dcb.mastermanagement_customizeprintid, dcb.mastermanagement_customizeprintname, dcb.StockDocumentNo, dcb.billmode, ISNULL(dcb.TotalGSTAmount,0) + ISNULL(dcb.totaltaxAmount,0) AS totaltaxAmount, ISNULL(dcb.MetalRate24K,0) AS MetalRate24K, dcb.isarchived, dcb.IsBillInYearWiseTBL, ISNULL(mc.PrintUnique, '') AS PrintUnique, ISNULL(dcb.Prefix, '') AS Prefix, ISNULL(dcb.Postfix, '') AS Postfix, ISNULL(dcb.billnoPrefixPostfix, 0) AS billnoPrefixPostfix, ISNULL(mc.IsReactPrint, 0) AS IsReactPrint ,case when isnull(TotalGSTAmount,0)+isnull(totaltaxAmount,0)>0 then 'With Tax' when isnull(TotalGSTAmount,0)+isnull(totaltaxAmount,0)<=0 then 'Without Tax' else '' end as taxfilter from [dbo].Stockmanagement_dcb as dcb with (nolock) LEFT JOIN [dbo].mastermanagement_customizeprint mc WITH (NOLOCK) ON dcb.mastermanagement_customizeprintid = mc.id where Stockmanagement_Statusid=16) AS CustomizePrintCTE 
							where isnull(isarchived,0)=0
						) as dcb
					on DI.StockDocumentNo=dcb.StockDocumentNo				
			left outer join (
							select sum(isnull(Solitair_Amount,0)) as Solitair_Amount
									,sum(isnull(Solitaire_AmountWithDiscount,0)) as Solitaire_AmountWithDiscount
									,sum(isnull(Solitair_Amount,0)-isnull(Solitaire_AmountWithDiscount,0)) as Solitaire_Discount	 
									,Stockmanagement_dcbdesignInfo_history_id
								from [dbo].[Inventorymanagement_solitaire_rate] with (nolock)
								where status_id=16
								group by Stockmanagement_dcbdesignInfo_history_id
							) soliator_tbl
						on DI.id = soliator_tbl.Stockmanagement_dcbdesignInfo_history_id
			left outer join (
						select Stockmanagement_dcbdesignInfo_history_id	
							,sum(iif(shape='gold',isnull(GrossDiamondWeight,0),0)) as Gold_Wt
							,sum(iif(shape='silver',isnull(GrossDiamondWeight,0),0)) as Silver_Wt
							,sum(iif(shape='platinum',isnull(GrossDiamondWeight,0),0)) as Platinum_Wt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(GrossDiamondWeight,0),0)) as Other_Wt
							,sum(iif(shape='gold',isnull(totaldiamond,0),0)) as Gold_Amt
							,sum(iif(shape='silver',isnull(totaldiamond,0),0)) as Silver_Amt
							,sum(iif(shape='platinum',isnull(totaldiamond,0),0)) as Platinum_Amt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(totaldiamond,0),0)) as Other_Amt
							,sum(iif(MasterManagement_DiamondStoneTypeid=4,isnull(PureWt,0),0)) as PureWt
							,sum(isnull(GrossDiamondWeight,0)*(ISNULL(metalWastage,0)/100)) as MM_Wastage
							,sum(iif(shape='gold',isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Gold_Wt
							,sum(iif(shape='silver',isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Silver_Wt
							,sum(iif(shape='platinum',isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Platinum_Wt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Other_Wt
							,sum(iif(shape='silver',isnull(PureWt,0),0)) as Pure_Silver_Wt
							,sum(iif(shape='platinum',isnull(PureWt,0),0)) as Pure_Platinum_Wt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(PureWt,0),0)) as Pure_Other_Wt
							,convert(decimal(38,2),sum(iif(isnull(metalWastage,0)=0 or MasterManagement_DiamondStoneTypeid=5
									,0
									,(isnull(GrossDiamondWeight,0)*isnull(DiamondRatePerCarat,0))-isnull(GrossDiamondWeight,0)*isnull(DiamondRatePerCarat,0)*(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)/(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)+isnull(metalWastage,0)))))) as MM_WastageAmount
							,convert(decimal(38,2),sum(iif(isnull(metalWastage,0)=0 or MasterManagement_DiamondStoneTypeid=4
									,0
									,(isnull(GrossDiamondWeight,0)*isnull(MetalRate,0))-isnull(GrossDiamondWeight,0)*isnull(MetalRate,0)*(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)/(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)+isnull(metalWastage,0)))))) as FindingWastageAmount					
						from
						(
							select Stockmanagement_dcbdesignInfo_history_id,MasterManagement_DiamondStoneTypeid
								,PureWt,GrossDiamondWeight,metalWastage,DiamondRatePerCarat,MMsize,shape,totaldiamond
							from [dbo].Stockmanagement_designdiamonddetail_history with (NoLock)
							where Statusid=16 
								and MasterManagement_DiamondStoneTypeid in (4,5)
						) as a
						INNER JOIN (
								select id,MetalRate
								from [dbo].[Stockmanagement_dcbdesignInfo_history] with (NoLock)
								where Stockmanagement_Statusid=16 
							) as h
						ON h.id = a.Stockmanagement_dcbdesignInfo_history_id
						group by Stockmanagement_dcbdesignInfo_history_id
					) as b
				on di.id=b.Stockmanagement_dcbdesignInfo_history_id

			left outer join (
				select a.Stockmanagement_dcbdesignInfo_history_id
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND isnull(a.Supplier,'')<> 'customer' THEN ISNULL(a.pieces, 0) ELSE 0 END) AS D_F_Pcs_Cm
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND isnull(a.Supplier,'')<> 'customer' THEN ISNULL(a.GrossDiamondWeight, 0) ELSE 0 END) AS D_F_Wt_Cm
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND a.supplier = 'customer' THEN ISNULL(a.pieces, 0) ELSE 0 END) AS D_F_Pcs_Ct
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND a.supplier = 'customer' THEN ISNULL(a.GrossDiamondWeight, 0) ELSE 0 END) AS D_F_Wt_Ct
				from [dbo].Stockmanagement_designdiamonddetail_history as a with (NoLock)
				where a.Statusid=16
				group by a.Stockmanagement_dcbdesignInfo_history_id
			) as bn
			on di.id=bn.Stockmanagement_dcbdesignInfo_history_id
			inner join (
							select id,MasterManagement_BusinessClassname 
							from [dbo].Usermanagement_systemloginmaster with(nolock)
						) as US
					on US.id = DI.usermanagement_customerid
			left join (
					SELECT StockBarcode,packageWt 
					From [dbo].Stockmanagement_dcbdesignInfo_PackageAddWt 
				) as BB 
				on BB.StockBarcode=DI.StockBarcode
		union all	
			Select DI.entrydate as [EntryDate]
				,DI.Stockmanagement_dcbdesignInfo_historyid as id
				,DI.entrydate as date
				,[DI].[StockBarcode]														
				,[DI].[designno]
				,[DI].[uniqueno]
				,[DI].StockDocumentNo as Stockdocumentno																																												
				,isnull(DI.usermanagement_customercode,'') as CustomerName
				,concat(DI.usermanagement_customerfirstname,' ',DI.usermanagement_customerlastname) as CustomerFullName
				,isnull(DI.mastermanagement_categoryname,'') as categoryname	
				,isnull(DI.mastermanagement_subcategoryname,'') as subcategoryname						
				,isnull(DI.MasterManagement_goldcolorname,'') as goldcolorname									
				,DI.Stockmanagement_Statusid
				,'' as statusname					
				,isnull(Di.StockBarcode,'') as jobno
				,replace(isnull(DI.StockBarcode,''),'_','') as jobno_excel
				,isnull(Di.job_skuno,'') as SKUNO
				,isnull(Di.autocode,'') as autocode
				,isnull(Di.design_DefaultImageName,'') as DefaultImageName
				,isnull(Di.imgrandomno,'') as imgrandomno
				,isnull(DI.mastermanagement_goldtypename,'') as goldtypename							
				,isnull(DI.IsERPreturn,0) as IsERPreturn
				,convert(decimal(38,2),isnull(DI.design_TotalAmouont,0)) as Amount
				,convert(decimal(38,2),isnull(DI.design_TotalAmouont,0)) as design_TotalAmouont 
				,isnull(DI.GrossCTWWithLoss,0) as grosswt										
				,convert(decimal(38,3),((isnull(DI.MetalDiamondWeightWithLoss,0)-isnull(DI.FindingWtWithLoss,0))*(ISNULL(DI.wastage,0)+ISNULL(DI.metalPriceRatio,0))/100))+isnull(DI.calculatemetalwtloss,0)+isnull(b.PureWt,0)+isnull(DI.PureFindingWt,0) as netwt_24k
				,isnull(DI.diamond_totalpieces,0) as dpcs
				,isnull(DI.DiamondCTWwithLoss,0) as dctw
				,isnull(di.colorstone_totalpieces,0) as cspcs
				,isnull(DI.ActualColorStoneWeight,0) as csctw
				,isnull(DI.totalmiscpcs,0) as miscpcs
				,isnull(DI.Totalmiscappliedweight,0) as miscwt
				,case when isnull(DI.jobflowfrom,'')='MFGJob' then 0 else 1 end as IsSupplierJob
				,1 as IsJobClosed
				,convert(decimal(38,2),isnull(TotalMetalAmount,0)+isnull(TotalFindingAmount,0)+isnull(MM_TotalMetalAmount,0)) as MetalAmount

					,convert(decimal(38,2),isnull(DI.TotalDiamondCost,0)) as DiamondAmount
					,convert(decimal(38,2),isnull(DI.TotalColorStoneCost,0)) as ColorStoneAmount
					,convert(decimal(38,2),isnull(TotalMakingAmount,0))
						+convert(decimal(38,2),isnull(totaldiasettingcost,0))
						+convert(decimal(38,2),isnull(totalCSsettingcost,0))
						+convert(decimal(38,2),isnull(TotalFindingSettingCost,0))
						+convert(decimal(38,2),isnull(MM_TotalMakingAmount,0)) as LabourAmount
					,convert(decimal(38,2),isnull(DI.TotalMiscCost,0)+convert(decimal(38,3),isnull(DI.TotalWastageAmount,0))+isnull(DI.OtherCharges,0)+(isnull(TotalDiamondHandling,0)))  as OtherAmount
					,convert(decimal(38,2),isnull(DI.UnitCost,0)) as UnitCost
					,isnull(DI.Mastermanagement_FG_StockLockername,'')as Mastermanagement_FG_StockLockername
					,1 as Isarchive
					,isnull(DI.Stockmanagement_QCStatusid,0) as QCStatusid
					,isnull(DI.Stockmanagement_QCStatusname,'') as QCStatusname 
					,(CASE(isWithHallMark) WHEN 1 THEN 'With HallMark' WHEN 2 THEN 'Without HallMark' 
										ELSE '' END )  as isWithHallMark
					,isnull(DI.MasterManagement_producttypeid,0) as producttypeid
					,isnull(DI.MasterManagement_producttypename,'') as producttypename
					,isnull(DI.[IsSampleLineJob],0) as IsSampleLineJob				
					,convert(decimal(38,2),isnull(case when isnull(isdiscountinamount,0)=1
										then isnull(Discount,0)
										else case when isnull(IsCriteriabasedAmount,0)=1
											then isnull(DiamondDiscountAmount,0)
												+ isnull(StoneDiscountAmount,0)
												+ isnull(MetalDiscountAmount,0)
												+ isnull(LabourDiscountAmount,0)
												+ isnull(SolitaireDiscountAmount,0)
												+ isnull(MiscDiscountAmount,0)
											else convert(decimal(38,3),((isnull(UnitCost,0)*isnull(Discount,0))/100.000))
											end
										end,0) + isnull(Solitaire_Discount,0))
							as Discount
					,isnull(DI.mastermanagement_collectionname,'') as collection
					,isnull(DI.usermanagement_customerid,0) as customerid
					,isnull(dcb.mastermanagement_customizeprintname,'') as customizeprintname
					,isnull(dcb.PrintUnique,'') as PrintUnique
					,isnull(dcb.totaltaxAmount,0) as totaltaxAmount
					,replace(isnull(DI.GroupJob,''),'_','') as GroupJob
					,convert(decimal(38,3),isnull(netweight,0)) as netwt
					,ISNULL([MetalRate24K],0) AS MetalRate24K
					,'' as IsshowCustomerName
					,ISNULL(di.TotalSettingCost,0) as TotalSettingCost
					,ISNULL(di.TotalDiamondHandling,0) as TotalDiamondHandling
					,0 as D_Pcs_Cm
					,0 as D_Wt_Cm
					,0 as D_Pcs_Ct
					,0 as D_Wt_Ct
					,0 as C_Pcs_Cm
					,0 as C_Wt_Cm
					,0 as C_Pcs_Ct
					,0 as C_Wt_Ct
					,dcb.Prefix
					,dcb.Postfix
					,dcb.billnoPrefixPostfix
					,dcb.IsReactPrint
					,ISNULL(DI.metalPriceRatio,0) as Tunch
					,ISNULL(DI.wastage,0) as Wastage
					,isnull(Metal_Type_Name,'') as Metal_Type_Name
					,isnull(MetalDiamondWeightWithLoss,0)+isnull(MM_MetalWeightWithLoss,0)-isnull(NetWeight,0) as MetalLoss
					,isnull(MetalDiamondWeightWithLoss,0)+isnull(MM_MetalWeightWithLoss,0) as NetWtWithLoss
					,J_Jobno

					,isnull(ArticleMasterCode,'') as ArticleMasterCode
					,taxfilter
					,billmode
					,orderform
					,Manufacturer
					,mastermanagement_brandname
					,US.MasterManagement_BusinessClassname	as MasterManagement_BusinessClassname
					,DI.usermanagement_salesrepcode			as usermanagement_salesrepcode
					,ISNULL(BB.packageWt,0)					as packageWt
					,convert(decimal(38,3),((isnull(DI.MetalDiamondWeightWithLoss,0)-isnull(DI.FindingWtWithLoss,0))*(ISNULL(DI.wastage,0))/100))+isnull(b.MM_Wastage,0) as Wastage_24k
					,iif(Metal_Type_Name='gold',MWt,0)+isnull(Gold_Wt,0) as GoldWt
					,iif(Metal_Type_Name='silver',MWt,0)+isnull(Silver_Wt,0) as SilverWt
					,iif(Metal_Type_Name='platinum',MWt,0)+isnull(Platinum_Wt,0) as PlatinumWt
					,iif(Metal_Type_Name not in ('gold','silver','platinum'),MWt,0)+isnull(Other_Wt,0) as OtherWt
					,convert(decimal(38,2),iif(Metal_Type_Name='gold',TotalMetalAmount,0)+isnull(Gold_Amt,0)) as GoldAmt
					,convert(decimal(38,2),iif(Metal_Type_Name='silver',TotalMetalAmount,0)+isnull(Silver_Amt,0)) as SilverAmt
					,convert(decimal(38,2),iif(Metal_Type_Name='platinum',TotalMetalAmount,0)+isnull(Platinum_Amt,0)) as PlatinumAmt
					,convert(decimal(38,2),iif(Metal_Type_Name not in ('gold','silver','platinum'),TotalMetalAmount,0)+isnull(Other_Amt,0)) as OtherAmt
					,MM_Wastage_Gold_Wt,MM_Wastage_Silver_Wt,MM_Wastage_Platinum_Wt,MM_Wastage_Other_Wt
					,Pure_Gold_Wt
					,Pure_Silver_Wt,Pure_Platinum_Wt,Pure_Other_Wt
					,jewellerysize
					,convert(decimal(38,2),iif(isnull(metalPriceRatio,0)+isnull(wastage,0)=0
							,0
							,isnull(TotalMetalAmount,0)-convert(decimal(38,3),isnull(MetalDiamondWeightWithLoss,0)*(isnull(MetalRate,0)*(isnull(metalPriceRatio,0)/(isnull(metalPriceRatio,0)+isnull(wastage,0)))))
						)) as WastageAmount
					,VersionName
					,ISNULL(bn.D_F_Pcs_Cm, 0) AS D_F_Pcs_Cm
					,ISNULL(bn.D_F_Wt_Cm, 0) AS D_F_Wt_Cm
					,ISNULL(bn.D_F_Pcs_Ct, 0) AS D_F_Pcs_Ct
					,ISNULL(bn.D_F_Wt_Ct, 0) AS D_F_Wt_Ct

			from
			(
				select Stockmanagement_dcbdesignInfo_historyid,StockDocumentNo,EntryDate,StockBarcode,designno,uniqueno,usermanagement_customercode,usermanagement_customerfirstname
					,mastermanagement_categoryname,usermanagement_customerlastname,mastermanagement_subcategoryname,MasterManagement_goldcolorname
					,Stockmanagement_Statusid,Job_SKUNo,autocode,design_defaultimagename,imgrandomno,mastermanagement_goldtypename,IsERPReturn
					,design_TotalAmouont,GrossCTWWithLoss,MetalDiamondWeightWithLoss,FindingWtWithLoss,wastage,metalPriceRatio,calculatemetalwtloss
					,PureFindingWt,diamond_totalpieces,DiamondCTWwithLoss,colorstone_totalpieces,ActualColorStoneWeight,totalmiscpcs
					,Totalmiscappliedweight,jobflowfrom,IsJobClosed,TotalMetalAmount,MM_TotalMetalAmount,TotalDiamondCost,TotalColorStoneCost
					,TotalMakingAmount,TotalDiaSettingCost,TotalCSSettingCost,TotalFindingSettingCost,TotalPriorityChargesAmount
					,TotalMiscCost,TotalWastageAmount,OtherCharges,TotalDiamondHandling,UnitCost,Mastermanagement_FG_StockLockername
					,Stockmanagement_QCStatusid,Stockmanagement_QCStatusname,isWithHallMark,MasterManagement_producttypeid
					,MasterManagement_producttypename,IsSampleLineJob,isdiscountinamount,Discount,IsCriteriabasedAmount,DiamondDiscountAmount
					,MetalDiscountAmount,StoneDiscountAmount,LabourDiscountAmount,SolitaireDiscountAmount,MiscDiscountAmount
					,mastermanagement_collectionname,usermanagement_customerid,GroupJob,NetWeight,TotalSettingCost,Metal_Type_Name
					,MM_MetalWeightWithLoss,J_JobNo,MM_TotalMakingAmount,ArticleMasterCode
					,case when (isnull(b2c_orderno,'')='' and isnull(b2c_orderno_stock,'')='')  then 'offline order'
						when (isnull(b2c_orderno,'')<>'' or isnull(b2c_orderno_stock,'')<>'')  then 'online order'
					end as orderform
					,CASE WHEN isnull(DI.Stock_Contractorcode,'') <> '' THEN isnull(DI.Stock_Contractorcode,'')
						ELSE  isnull(DI.Stock_Suppliercode,'') END AS Manufacturer
					,mastermanagement_brandname
					,usermanagement_salesrepcode
					,MetalRate,convert(decimal(38,3),isnull(netweight,0)-isnull(MM_MetalWeightWithLoss,0)-isnull(FindingWtWithLoss,0)) as MWt
					,TotalFindingAmount
					,convert(decimal(38,3),((isnull(DI.MetalDiamondWeightWithLoss,0)+isnull(DI.FindingWtWithLoss,0))
							*(ISNULL(DI.wastage,0)+ISNULL(DI.metalPriceRatio,0))/100))
							+isnull(DI.calculatemetalwtloss,0)					
							as Pure_Gold_Wt
					,jewellerysize
					,ISNULL(J_Version,'') as VersionName
				FROM [dbo].[Stockmanagement_dcbdesignInfo_history_Archive]  as DI with (nolock)
				where Stockmanagement_Statusid=16
				and 1=1 
				and isnull([DI].StockDocumentNo,'')<>''
			) as DI

			inner join (select * FROM (SELECT dcb.mastermanagement_customizeprintid, dcb.mastermanagement_customizeprintname, dcb.StockDocumentNo, dcb.billmode, ISNULL(dcb.TotalGSTAmount,0) + ISNULL(dcb.totaltaxAmount,0) AS totaltaxAmount, ISNULL(dcb.MetalRate24K,0) AS MetalRate24K, dcb.isarchived, dcb.IsBillInYearWiseTBL, ISNULL(mc.PrintUnique, '') AS PrintUnique, ISNULL(dcb.Prefix, '') AS Prefix, ISNULL(dcb.Postfix, '') AS Postfix, ISNULL(dcb.billnoPrefixPostfix, 0) AS billnoPrefixPostfix, ISNULL(mc.IsReactPrint, 0) AS IsReactPrint ,case when isnull(TotalGSTAmount,0)+isnull(totaltaxAmount,0)>0 then 'With Tax' when isnull(TotalGSTAmount,0)+isnull(totaltaxAmount,0)<=0 then 'Without Tax' else '' end as taxfilter from [dbo].Stockmanagement_dcb as dcb with (nolock) LEFT JOIN [dbo].mastermanagement_customizeprint mc WITH (NOLOCK) ON dcb.mastermanagement_customizeprintid = mc.id where Stockmanagement_Statusid=16) AS CustomizePrintCTE 
								where isarchived=1 and isnull(IsBillInYearWiseTBL,0)<>1) as dcb
						  on DI.StockDocumentNo=dcb.StockDocumentNo
			left outer join	(
								select sum(isnull(Solitair_Amount,0)) as Solitair_Amount
										,sum(isnull(Solitaire_AmountWithDiscount,0)) as Solitaire_AmountWithDiscount
										,sum(isnull(Solitair_Amount,0)-isnull(Solitaire_AmountWithDiscount,0)) as Solitaire_Discount	 
										,Stockmanagement_dcbdesignInfo_history_id
									from [dbo].[Inventorymanagement_solitaire_rate] with (nolock)
									where status_id=16
									group by Stockmanagement_dcbdesignInfo_history_id
								) soliator_tbl
							on DI.Stockmanagement_dcbdesignInfo_historyid = soliator_tbl.Stockmanagement_dcbdesignInfo_history_id
			left outer join (
						select Stockmanagement_dcbdesignInfo_history_id
							,sum(iif(MasterManagement_DiamondStoneTypeid=4,isnull(PureWt,0),0)) as PureWt
							,sum(isnull(GrossDiamondWeight,0)*(ISNULL(metalWastage,0)/100)) as MM_Wastage
							,sum(iif(shape='gold',isnull(GrossDiamondWeight,0),0)) as Gold_Wt
							,sum(iif(shape='silver',isnull(GrossDiamondWeight,0),0)) as Silver_Wt
							,sum(iif(shape='platinum',isnull(GrossDiamondWeight,0),0)) as Platinum_Wt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(GrossDiamondWeight,0),0)) as Other_Wt
							,sum(iif(shape='gold',isnull(totaldiamond,0),0)) as Gold_Amt
							,sum(iif(shape='silver',isnull(totaldiamond,0),0)) as Silver_Amt
							,sum(iif(shape='platinum',isnull(totaldiamond,0),0)) as Platinum_Amt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(totaldiamond,0),0)) as Other_Amt
							,sum(iif(shape='gold',isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Gold_Wt
							,sum(iif(shape='silver',isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Silver_Wt
							,sum(iif(shape='platinum',isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Platinum_Wt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Other_Wt
							,sum(iif(shape='silver',isnull(PureWt,0),0)) as Pure_Silver_Wt
							,sum(iif(shape='platinum',isnull(PureWt,0),0)) as Pure_Platinum_Wt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(PureWt,0),0)) as Pure_Other_Wt
							,convert(decimal(38,2),sum(iif(isnull(metalWastage,0)=0 or MasterManagement_DiamondStoneTypeid=5
									,0
									,(isnull(GrossDiamondWeight,0)*isnull(DiamondRatePerCarat,0))-isnull(GrossDiamondWeight,0)*isnull(DiamondRatePerCarat,0)*(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)/(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)+isnull(metalWastage,0)))))) as MM_WastageAmount
							,convert(decimal(38,2),sum(iif(isnull(metalWastage,0)=0 or MasterManagement_DiamondStoneTypeid=4
									,0
									,(isnull(GrossDiamondWeight,0)*isnull(MetalRate,0))-isnull(GrossDiamondWeight,0)*isnull(MetalRate,0)*(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)/(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)+isnull(metalWastage,0)))))) as FindingWastageAmount					
						from
						(
							select Stockmanagement_dcbdesignInfo_history_id,MasterManagement_DiamondStoneTypeid
								,PureWt,GrossDiamondWeight,metalWastage,DiamondRatePerCarat,MMsize,shape,totaldiamond
							from [dbo].Stockmanagement_designdiamonddetail_history_Archive with (NoLock)
							where Statusid=16 and MasterManagement_DiamondStoneTypeid in (4,5)
						) as a

						INNER JOIN (
								select Stockmanagement_dcbdesignInfo_historyid as id,MetalRate
								from [dbo].[Stockmanagement_dcbdesignInfo_history_Archive] with (NoLock)
								where Stockmanagement_Statusid=16 
							) as h
						ON h.id = a.Stockmanagement_dcbdesignInfo_history_id
						group by Stockmanagement_dcbdesignInfo_history_id
					) as b
				on di.Stockmanagement_dcbdesignInfo_historyid=b.Stockmanagement_dcbdesignInfo_history_id
			left outer join (
				select a.Stockmanagement_dcbdesignInfo_history_id
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND isnull(a.Supplier,'')<> 'customer' THEN ISNULL(a.pieces, 0) ELSE 0 END) AS D_F_Pcs_Cm
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND isnull(a.Supplier,'')<> 'customer' THEN ISNULL(a.GrossDiamondWeight, 0) ELSE 0 END) AS D_F_Wt_Cm
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND a.supplier = 'customer' THEN ISNULL(a.pieces, 0) ELSE 0 END) AS D_F_Pcs_Ct
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND a.supplier = 'customer' THEN ISNULL(a.GrossDiamondWeight, 0) ELSE 0 END) AS D_F_Wt_Ct
				from [dbo].Stockmanagement_designdiamonddetail_history_Archive as a with (NoLock)
				where a.Statusid=16
				group by a.Stockmanagement_dcbdesignInfo_history_id
			) as bn
			on DI.Stockmanagement_dcbdesignInfo_historyid = bn.Stockmanagement_dcbdesignInfo_history_id
			inner join (
							select id,MasterManagement_BusinessClassname 
							from [dbo].Usermanagement_systemloginmaster with(nolock)
						) as US
					on US.id = DI.usermanagement_customerid
			left join (
					SELECT StockBarcode,packageWt 
					From [dbo].Stockmanagement_dcbdesignInfo_PackageAddWt 
				) as BB 
				on BB.StockBarcode=DI.StockBarcode

		union all	
				Select DI.entrydate as [EntryDate]
					,DI.Stockmanagement_dcbdesignInfo_historyid as id
					,DI.entrydate as date
					,[DI].[StockBarcode]														
					,[DI].[designno]
					,[DI].[uniqueno]
					,[DI].StockDocumentNo as Stockdocumentno																																												
					,isnull(DI.usermanagement_customercode,'') as CustomerName
					,concat(DI.usermanagement_customerfirstname,' ',DI.usermanagement_customerlastname) as CustomerFullName
					,isnull(DI.mastermanagement_categoryname,'') as categoryname	
					,isnull(DI.mastermanagement_subcategoryname,'') as subcategoryname						
					,isnull(DI.MasterManagement_goldcolorname,'') as goldcolorname									
					,DI.Stockmanagement_Statusid
					,'' as statusname					
					,isnull(Di.StockBarcode,'') as jobno
					,jobno_excel as jobno_excel
					,isnull(Di.job_skuno,'') as SKUNO
					,isnull(Di.autocode,'') as autocode
					,isnull(Di.design_DefaultImageName,'') as DefaultImageName
					,isnull(Di.imgrandomno,'') as imgrandomno
					,isnull(DI.mastermanagement_goldtypename,'') as goldtypename							
					,isnull(DI.IsERPreturn,0) as IsERPreturn
					,convert(decimal(38,2),isnull(DI.design_TotalAmouont,0)) as Amount
					,convert(decimal(38,2),isnull(DI.design_TotalAmouont,0)) as design_TotalAmouont 
					,isnull(DI.GrossCTWWithLoss,0) as grosswt										
					,convert(decimal(38,3),((isnull(DI.MetalDiamondWeightWithLoss,0)-isnull(DI.FindingWtWithLoss,0))*(ISNULL(DI.wastage,0)+ISNULL(DI.metalPriceRatio,0))/100))+isnull(DI.calculatemetalwtloss,0)+isnull(b.PureWt,0)+isnull(DI.PureFindingWt,0) as netwt_24k
					,isnull(DI.diamond_totalpieces,0) as dpcs
					,isnull(DI.DiamondCTWwithLoss,0) as dctw
					,isnull(di.colorstone_totalpieces,0) as cspcs
					,isnull(DI.ActualColorStoneWeight,0) as csctw
					,isnull(DI.totalmiscpcs,0) as miscpcs
					,isnull(DI.Totalmiscappliedweight,0) as miscwt
					,IsSupplierJob as IsSupplierJob
					,1 as IsJobClosed
					,convert(decimal(38,2),isnull(MetalAmount,0)) as MetalAmount 

		,convert(decimal(38,2),isnull(DI.DiamondAmount,0)) as DiamondAmount
					,convert(decimal(38,2),isnull(DI.ColorStoneAmount,0)) as ColorStoneAmount
					,convert(decimal(38,2),isnull(TotalMakingAmount,0))
						+convert(decimal(38,2),isnull(totaldiasettingcost,0))
						+convert(decimal(38,2),isnull(totalCSsettingcost,0))
						+convert(decimal(38,2),isnull(TotalFindingSettingCost,0))
						+convert(decimal(38,2),isnull(MM_TotalMakingAmount,0)) as LabourAmount
					,convert(decimal(38,2),isnull(OtherAmount,0))  as OtherAmount
					,convert(decimal(38,2),isnull(DI.UnitCost,0)) as UnitCost
					,isnull(DI.Mastermanagement_FG_StockLockername,'')as Mastermanagement_FG_StockLockername
					,1 as Isarchive
					,isnull(DI.Stockmanagement_QCStatusid,0) as QCStatusid
					,isnull(DI.Stockmanagement_QCStatusname,'') as QCStatusname 
					,(CASE(isWithHallMark) WHEN 1 THEN 'With HallMark' WHEN 2 THEN 'Without HallMark' 
										ELSE '' END )  as isWithHallMark
					,isnull(DI.MasterManagement_producttypeid,0) as producttypeid
					,isnull(DI.MasterManagement_producttypename,'') as producttypename
					,isnull(DI.[IsSampleLineJob],0) as IsSampleLineJob				
					,convert(decimal(38,2),isnull(Discount,0)+isnull(Solitaire_Discount,0))as Discount
					,isnull(DI.mastermanagement_collectionname,'') as collection
					,isnull(DI.usermanagement_customerid,0) as customerid
					,isnull(dcb.mastermanagement_customizeprintname,'') as customizeprintname
					,isnull(dcb.PrintUnique,'') as PrintUnique
					,isnull(dcb.totaltaxAmount,0) as totaltaxAmount
					,replace(isnull(DI.GroupJob,''),'_','') as GroupJob
					,convert(decimal(38,3),isnull(netweight,0)) as netwt
					,ISNULL([MetalRate24K],0) AS MetalRate24K
					,'' as IsshowCustomerName
					,ISNULL(di.TotalSettingCost,0) as TotalSettingCost
					,ISNULL(di.TotalDiamondHandling,0) as TotalDiamondHandling
					,0 as D_Pcs_Cm
					,0 as D_Wt_Cm
					,0 as D_Pcs_Ct
					,0 as D_Wt_Ct
					,0 as C_Pcs_Cm
					,0 as C_Wt_Cm
					,0 as C_Pcs_Ct
					,0 as C_Wt_Ct
					,dcb.Prefix
					,dcb.Postfix
					,dcb.billnoPrefixPostfix
					,dcb.IsReactPrint
					,ISNULL(DI.metalPriceRatio,0) as Tunch
					,ISNULL(DI.wastage,0) as Wastage
					,isnull(Metal_Type_Name,'') as Metal_Type_Name
					,isnull(MetalDiamondWeightWithLoss,0)+isnull(MM_MetalWeightWithLoss,0)-isnull(NetWeight,0) as MetalLoss
					,isnull(MetalDiamondWeightWithLoss,0)+isnull(MM_MetalWeightWithLoss,0) as NetWtWithLoss
					,J_Jobno
					,isnull(ArticleMasterCode,'') as ArticleMasterCode
					,taxfilter
					,billmode
					,orderform
					,Manufacturer
					,mastermanagement_brandname
					,US.MasterManagement_BusinessClassname	as MasterManagement_BusinessClassname
					,U.customercode							as usermanagement_salesrepcode
					,ISNULL(BB.packageWt,0)					as packageWt
					,convert(decimal(38,3),((isnull(DI.MetalDiamondWeightWithLoss,0)-isnull(DI.FindingWtWithLoss,0))*(ISNULL(DI.wastage,0))/100))+isnull(b.MM_Wastage,0) as Wastage_24k
					,iif(Metal_Type_Name='gold',MWt,0)+isnull(Gold_Wt,0) as GoldWt
					,iif(Metal_Type_Name='silver',MWt,0)+isnull(Silver_Wt,0) as SilverWt
					,iif(Metal_Type_Name='platinum',MWt,0)+isnull(Platinum_Wt,0) as PlatinumWt
					,iif(Metal_Type_Name not in ('gold','silver','platinum'),MWt,0)+isnull(Other_Wt,0) as OtherWt
					,convert(decimal(38,2),iif(Metal_Type_Name='gold',TotalMetalAmount,0)+isnull(Gold_Amt,0)) as GoldAmt
					,convert(decimal(38,2),iif(Metal_Type_Name='silver',TotalMetalAmount,0)+isnull(Silver_Amt,0)) as SilverAmt
					,convert(decimal(38,2),iif(Metal_Type_Name='platinum',TotalMetalAmount,0)+isnull(Platinum_Amt,0)) as PlatinumAmt

					,convert(decimal(38,2),iif(Metal_Type_Name not in ('gold','silver','platinum'),TotalMetalAmount,0)+isnull(Other_Amt,0)) as OtherAmt
					,MM_Wastage_Gold_Wt,MM_Wastage_Silver_Wt,MM_Wastage_Platinum_Wt,MM_Wastage_Other_Wt
					,Pure_Gold_Wt
					,Pure_Silver_Wt,Pure_Platinum_Wt,Pure_Other_Wt
					,jewellerysize
					,convert(decimal(38,2),iif(isnull(metalPriceRatio,0)+isnull(wastage,0)=0
							,0
							,isnull(TotalMetalAmount,0)-convert(decimal(38,3),isnull(MetalDiamondWeightWithLoss,0)*(isnull(MetalRate,0)*(isnull(metalPriceRatio,0)/(isnull(metalPriceRatio,0)+isnull(wastage,0)))))
						)) as WastageAmount
					,VersionName
					,ISNULL(bn.D_F_Pcs_Cm, 0) AS D_F_Pcs_Cm
					,ISNULL(bn.D_F_Wt_Cm, 0) AS D_F_Wt_Cm
					,ISNULL(bn.D_F_Pcs_Ct, 0) AS D_F_Pcs_Ct
					,ISNULL(bn.D_F_Wt_Ct, 0) AS D_F_Wt_Ct
		from
			(
				select Stockmanagement_dcbdesignInfo_historyid,StockDocumentNo,EntryDate,StockBarcode,designno,uniqueno,usermanagement_customercode,usermanagement_customerfirstname
					,mastermanagement_categoryname,usermanagement_customerlastname,mastermanagement_subcategoryname,MasterManagement_goldcolorname
					,Stockmanagement_Statusid,Job_SKUNo,autocode,design_defaultimagename,imgrandomno,mastermanagement_goldtypename,IsERPReturn
					,design_TotalAmouont,GrossCTWWithLoss,MetalDiamondWeightWithLoss,FindingWtWithLoss,wastage,metalPriceRatio,calculatemetalwtloss
					,PureFindingWt,diamond_totalpieces,DiamondCTWwithLoss,colorstone_totalpieces,ActualColorStoneWeight,totalmiscpcs
					,Totalmiscappliedweight,MetalAmount,TotalMakingAmount,TotalDiaSettingCost,TotalCSSettingCost,TotalFindingSettingCost
					,TotalDiamondHandling,UnitCost,Mastermanagement_FG_StockLockername
					,Stockmanagement_QCStatusid,Stockmanagement_QCStatusname,isWithHallMark,MasterManagement_producttypeid
					,MasterManagement_producttypename,IsSampleLineJob,Discount
					,mastermanagement_collectionname,usermanagement_customerid,GroupJob,NetWeight,TotalSettingCost,Metal_Type_Name
					,MM_MetalWeightWithLoss,J_JobNo,jobno_excel,IsSupplierJob,DiamondAmount,ColorStoneAmount,OtherAmount
					,MM_TotalMakingAmount,'' as ArticleMasterCode
					,case when (isnull(b2c_orderno,'')='' and isnull(b2c_orderno_stock,'')='')  then 'offline order'
						when (isnull(b2c_orderno,'')<>'' or isnull(b2c_orderno_stock,'')<>'')  then 'online order'
					end as orderform
					,CASE WHEN isnull(DI.Stock_Contractorcode,'') <> '' THEN isnull(DI.Stock_Contractorcode,'')
						ELSE  isnull(DI.Stock_Suppliercode,'') END AS Manufacturer
					,mastermanagement_brandname
					,usermanagement_salesrepid
					,MetalRate,convert(decimal(38,3),isnull(netweight,0)-isnull(MM_MetalWeightWithLoss,0)-isnull(FindingWtWithLoss,0)) as MWt
					,convert(decimal(38,3),((isnull(DI.MetalDiamondWeightWithLoss,0)+isnull(DI.FindingWtWithLoss,0))
							*(ISNULL(DI.wastage,0)+ISNULL(DI.metalPriceRatio,0))/100))
							+isnull(DI.calculatemetalwtloss,0)					
							as Pure_Gold_Wt
					,'' as jewellerysize
					,TotalMetalAmount
					,ISNULL(J_Version,'') as VersionName
				FROM [dbo].[SideUp_Sales_Report_Job]  as DI with (nolock)
				where Stockmanagement_Statusid=16
				and 1=1 
				and isnull([DI].StockDocumentNo,'')<>''
			) as DI

		inner join (select * FROM (SELECT dcb.mastermanagement_customizeprintid, dcb.mastermanagement_customizeprintname, dcb.StockDocumentNo, dcb.billmode, ISNULL(dcb.TotalGSTAmount,0) + ISNULL(dcb.totaltaxAmount,0) AS totaltaxAmount, ISNULL(dcb.MetalRate24K,0) AS MetalRate24K, dcb.isarchived, dcb.IsBillInYearWiseTBL, ISNULL(mc.PrintUnique, '') AS PrintUnique, ISNULL(dcb.Prefix, '') AS Prefix, ISNULL(dcb.Postfix, '') AS Postfix, ISNULL(dcb.billnoPrefixPostfix, 0) AS billnoPrefixPostfix, ISNULL(mc.IsReactPrint, 0) AS IsReactPrint ,case when isnull(TotalGSTAmount,0)+isnull(totaltaxAmount,0)>0 then 'With Tax' when isnull(TotalGSTAmount,0)+isnull(totaltaxAmount,0)<=0 then 'Without Tax' else '' end as taxfilter from [dbo].Stockmanagement_dcb as dcb with (nolock) LEFT JOIN [dbo].mastermanagement_customizeprint mc WITH (NOLOCK) ON dcb.mastermanagement_customizeprintid = mc.id where Stockmanagement_Statusid=16) AS CustomizePrintCTE 
								where isarchived=1 and IsBillInYearWiseTBL=1) as dcb
						  on DI.StockDocumentNo=dcb.StockDocumentNo
			left outer join	(
								select sum(isnull(Solitair_Amount,0)) as Solitair_Amount
										,sum(isnull(Solitaire_AmountWithDiscount,0)) as Solitaire_AmountWithDiscount
										,sum(isnull(Solitair_Amount,0)-isnull(Solitaire_AmountWithDiscount,0)) as Solitaire_Discount	 
										,Stockmanagement_dcbdesignInfo_history_id
									from [dbo].[Inventorymanagement_solitaire_rate] with (nolock)
									where status_id=16
									group by Stockmanagement_dcbdesignInfo_history_id
								) soliator_tbl
							on DI.Stockmanagement_dcbdesignInfo_historyid = soliator_tbl.Stockmanagement_dcbdesignInfo_history_id
			left outer join (						
						select 
							Stockmanagement_dcbdesignInfo_history_id
							,sum(iif(MasterManagement_DiamondStoneTypeid=4,isnull(PureWt,0),0)) as PureWt
							,sum(isnull(GrossDiamondWeight,0)*(ISNULL(metalWastage,0)/100)) as MM_Wastage
							,sum(iif(shape='gold',isnull(GrossDiamondWeight,0),0)) as Gold_Wt
							,sum(iif(shape='silver',isnull(GrossDiamondWeight,0),0)) as Silver_Wt
							,sum(iif(shape='platinum',isnull(GrossDiamondWeight,0),0)) as Platinum_Wt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(GrossDiamondWeight,0),0)) as Other_Wt
							,sum(iif(shape='gold',isnull(totaldiamond,0),0)) as Gold_Amt
							,sum(iif(shape='silver',isnull(totaldiamond,0),0)) as Silver_Amt
							,sum(iif(shape='platinum',isnull(totaldiamond,0),0)) as Platinum_Amt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(totaldiamond,0),0)) as Other_Amt
							,sum(iif(shape='gold',isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Gold_Wt
							,sum(iif(shape='silver',isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Silver_Wt
							,sum(iif(shape='platinum',isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Platinum_Wt
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(GrossDiamondWeight,0)*ISNULL(metalWastage,0),0)) as MM_Wastage_Other_Wt
							,sum(iif(shape='silver',isnull(PureWt,0),0)) as Pure_Silver_Wt
							,sum(iif(shape='platinum',isnull(PureWt,0),0)) as Pure_Platinum_Wt	
							,sum(iif(shape not in ('gold','silver','platinum'),isnull(PureWt,0),0)) as Pure_Other_Wt
                            ,convert(decimal(38,2),sum(iif(isnull(metalWastage,0)=0 or MasterManagement_DiamondStoneTypeid=5
									,0
									,(isnull(GrossDiamondWeight,0)*isnull(DiamondRatePerCarat,0))-isnull(GrossDiamondWeight,0)*isnull(DiamondRatePerCarat,0)*(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)/(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)+isnull(metalWastage,0)))))) as MM_WastageAmount
							,convert(decimal(38,2),sum(iif(isnull(metalWastage,0)=0 or MasterManagement_DiamondStoneTypeid=4
									,0
									,(isnull(GrossDiamondWeight,0)*isnull(MetalRate,0))-isnull(GrossDiamondWeight,0)*isnull(MetalRate,0)*(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)/(isnull(TRY_PARSE(TRIM(MMsize) as decimal(38,3)),0)+isnull(metalWastage,0)))))) as FindingWastageAmount					
						from
						(
                            select Stockmanagement_dcbdesignInfo_history_id,MasterManagement_DiamondStoneTypeid
								,PureWt,GrossDiamondWeight,metalWastage,DiamondRatePerCarat,MMsize,shape,totaldiamond
							from [dbo].SideUp_Sales_Report_MFDetail with (NoLock)
							where MasterManagement_DiamondStoneTypeid in (4,5)
                        ) as a
                        INNER JOIN (
								select Stockmanagement_dcbdesignInfo_historyid as id,MetalRate
								from [dbo].[SideUp_Sales_Report_Job] with (NoLock) 
							) as h

						ON h.id = a.Stockmanagement_dcbdesignInfo_history_id
						group by Stockmanagement_dcbdesignInfo_history_id
					) as b
				on di.Stockmanagement_dcbdesignInfo_historyid=b.Stockmanagement_dcbdesignInfo_history_id
			left outer join (
				select a.Stockmanagement_dcbdesignInfo_history_id
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND isnull(a.Supplier,'')<> 'customer' THEN ISNULL(a.pieces, 0) ELSE 0 END) AS D_F_Pcs_Cm
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND isnull(a.Supplier,'')<> 'customer' THEN ISNULL(a.GrossDiamondWeight, 0) ELSE 0 END) AS D_F_Wt_Cm
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND a.supplier = 'customer' THEN ISNULL(a.pieces, 0) ELSE 0 END) AS D_F_Pcs_Ct
					,SUM(CASE WHEN a.MasterManagement_DiamondStoneTypeid = 1 AND a.supplier = 'customer' THEN ISNULL(a.GrossDiamondWeight, 0) ELSE 0 END) AS D_F_Wt_Ct
				from [dbo].SideUp_Sales_Report_DiaCSDetail as a with (NoLock)
				where a.MasterManagement_DiamondStoneTypeid=1
				group by a.Stockmanagement_dcbdesignInfo_history_id
			) as bn
			on di.Stockmanagement_dcbdesignInfo_historyid=bn.Stockmanagement_dcbdesignInfo_history_id
			left join (
					select id,customercode
					from [dbo].Usermanagement_systemloginmaster
				) as U
				on U.id=DI.usermanagement_salesrepid
			inner join (
							select id,MasterManagement_BusinessClassname 
							from [dbo].Usermanagement_systemloginmaster with(nolock)
						) as US
					on US.id = DI.usermanagement_customerid

				left join (
					SELECT StockBarcode,packageWt 
					From [dbo].Stockmanagement_dcbdesignInfo_PackageAddWt 
				) as BB 
				on BB.StockBarcode=DI.StockBarcode
			) as a

		
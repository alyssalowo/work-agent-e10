[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$ErrorActionPreference = 'Stop'
try {
    . "$PSScriptRoot\load-client.ps1"
    Add-Type -Path "$PSScriptRoot\ShipmentReader.cs" -ReferencedAssemblies $e10References
    $e10Session = New-Object E10AccountSession($e10ConnectionConfiguration.server_url)
    while ($null -ne ($e10Line = [Console]::ReadLine())) {
        try {
            $e10Request = $e10Line | ConvertFrom-Json
            $e10Operation = [string]$e10Request.operation
            $e10CredentialFile = "$PSScriptRoot\credentials.dpapi"
            switch ($e10Operation) {
                'status' {
                    $e10Result = $e10Session.Status() | ConvertFrom-Json
                    $e10Result | Add-Member -NotePropertyName credentials_configured -NotePropertyValue (Test-Path -LiteralPath $e10CredentialFile)
                }
                'login' {
                    if (-not (Test-Path -LiteralPath $e10CredentialFile)) {
                        $e10Result = @{ ok=$false; error='请先在本机 E10 MCP 配置窗口保存账号。' }
                    } else {
                        $e10Credential = [E10CredentialStore]::Load($e10CredentialFile)
                        $e10Password = $e10Credential.Password
                        try {
                            if (-not [string]::IsNullOrEmpty($e10Credential.Identity)) {
                                $e10Result = $e10Session.LoginIdentity($e10Credential.UserName, $e10Credential.Identity, $e10ConnectionConfiguration.language) | ConvertFrom-Json
                            } else {
                                $e10Result = $e10Session.Login($e10Credential.UserName, $e10Password, $e10ConnectionConfiguration.language) | ConvertFrom-Json
                            }
                        }
                        finally { $e10Password=$null; $e10Credential=$null }
                    }
                }
                'current_user' { $e10Result = $e10Session.CurrentUser() | ConvertFrom-Json }
                'logout' { $e10Result = $e10Session.Logout() | ConvertFrom-Json }
                'shipment_configuration' { $e10Result = [ShipmentReader]::Configuration($e10Session,[string]$e10Request.parameters.first,[string]$e10Request.parameters.second) | ConvertFrom-Json }
                'query_shipment_details' { $e10Result = [ShipmentReader]::Page($e10Session,[string]$e10Request.parameters.options_json) | ConvertFrom-Json }
                'module_access' { $e10Result = $e10Session.ModuleAccess() | ConvertFrom-Json }
                'query_workorder' { $e10Result = $e10Session.QueryWorkorder([string]$e10Request.parameters.workorder_no) | ConvertFrom-Json }
                'query_workorder_workflow' { $e10Result = $e10Session.QueryWorkorderWorkflow([string]$e10Request.parameters.planner_name,[string]$e10Request.parameters.doc_type_code,[string]$e10Request.parameters.approval_status,[string]$e10Request.parameters.doc_date,[int]$e10Request.parameters.start_row,[int]$e10Request.parameters.max_rows) | ConvertFrom-Json }
                'prepare_workorder_confirmation_batch' { $e10Result = $e10Session.PrepareWorkorderConfirmationBatch([string]$e10Request.parameters.expected_items_json,[string]$e10Request.parameters.planner_name,[string]$e10Request.parameters.doc_type_code,[string]$e10Request.parameters.doc_date,[string]$e10Request.parameters.selection_scope,[string]$e10Request.parameters.selected_ids_json,[string]$e10Request.parameters.excluded_ids_json) | ConvertFrom-Json }
                'query_packaging_workorders' { $e10Result = $e10Session.QueryPackagingWorkorders([string]$e10Request.parameters.prefix,[string]$e10Request.parameters.date_from,[int]$e10Request.parameters.start_row,[int]$e10Request.parameters.max_rows) | ConvertFrom-Json }
                'query_picking_requests' { $e10Result = $e10Session.QueryPickingRequests([string]$e10Request.parameters.workorder_no, [int]$e10Request.parameters.max_rows) | ConvertFrom-Json }
                'preview_picking_request' { $e10Result = $e10Session.PreviewPickingRequest([string]$e10Request.parameters.workorder_no) | ConvertFrom-Json }
                'get_document' { $e10Result = $e10Session.GetDocument([string]$e10Request.parameters.module, [string]$e10Request.parameters.document_no) | ConvertFrom-Json }
                'document_schema' { $e10Result = $e10Session.DocumentSchema([string]$e10Request.parameters.module) | ConvertFrom-Json }
                'lookup_master' { $e10Result = $e10Session.LookupMaster([string]$e10Request.parameters.kind,[string]$e10Request.parameters.code) | ConvertFrom-Json }
                'lookup_item_feature' { $e10Result = $e10Session.LookupItemFeature([string]$e10Request.parameters.item_code,[string]$e10Request.parameters.feature_code) | ConvertFrom-Json }
                'preview_manual_workorder' { $e10Result = $e10Session.PreviewManualWorkorder([string]$e10Request.parameters.item_code,[string]$e10Request.parameters.feature_code,[string]$e10Request.parameters.plant_id,[decimal]$e10Request.parameters.quantity,[string]$e10Request.parameters.doc_date,[string]$e10Request.parameters.start_date,[string]$e10Request.parameters.complete_date) | ConvertFrom-Json }
                'prepare_manual_workorder' { $e10Result = $e10Session.PrepareManualWorkorder([string]$e10Request.parameters.item_code,[string]$e10Request.parameters.feature_code,[string]$e10Request.parameters.plant_id,[decimal]$e10Request.parameters.quantity,[string]$e10Request.parameters.doc_date,[string]$e10Request.parameters.start_date,[string]$e10Request.parameters.complete_date,[string]$e10Request.parameters.reference) | ConvertFrom-Json }
                'prepare_document' { $e10Result = $e10Session.PrepareDocument([string]$e10Request.parameters.module,[string]$e10Request.parameters.action,[string]$e10Request.parameters.document_no,[string]$e10Request.parameters.payload_json) | ConvertFrom-Json }
                'prepare_order_workorders' { $e10Result = $e10Session.PrepareOrderWorkorders([string]$e10Request.parameters.order_no,[string]$e10Request.parameters.plant_id,[string]$e10Request.parameters.options_json,[string]$e10Request.parameters.selected_json,$e10ConnectionConfiguration.client_directory) | ConvertFrom-Json }
                'query_order_workorder_sources' { $e10Result = $e10Session.QueryOrderWorkorderSources([string]$e10Request.parameters.plant_name,[string]$e10Request.parameters.date_from,[string]$e10Request.parameters.departments_json,[string]$e10Request.parameters.tokens_json,[int]$e10Request.parameters.start_row,[int]$e10Request.parameters.max_rows,$e10ConnectionConfiguration.client_directory) | ConvertFrom-Json }
                'prepare_packaging_order_batch' { $e10Result = $e10Session.PreparePackagingOrderBatch([string]$e10Request.parameters.numbers_json,[string]$e10Request.parameters.plant_name,[string]$e10Request.parameters.doc_type_code,[string]$e10Request.parameters.v_json,[string]$e10Request.parameters.h_json,$e10ConnectionConfiguration.client_directory) | ConvertFrom-Json }
                'query_order_versions' { $e10Result = $e10Session.QueryOrderHeadVersions([string]$e10Request.parameters.numbers_json) | ConvertFrom-Json }
                'query_order_workorder_result' { $e10Result = $e10Session.QueryOrderWorkorderResult([string]$e10Request.parameters.source_ids_json) | ConvertFrom-Json }
                'query_workorder_item_date' { $e10Result = $e10Session.QueryWorkorderItemDate([string]$e10Request.parameters.plant_name,[string]$e10Request.parameters.doc_type_code,[string]$e10Request.parameters.item_codes_json,[string]$e10Request.parameters.doc_date) | ConvertFrom-Json }
                'prepare_picking_generation' { $e10Result = $e10Session.PreparePickingGeneration([string]$e10Request.parameters.workorder_no,[string]$e10Request.parameters.options_json) | ConvertFrom-Json }
                'resolve_picking_applicant' { $e10Result = $e10Session.ResolvePickingApplicant([string]$e10Request.parameters.applicant_name,[string]$e10Request.parameters.apply_date) | ConvertFrom-Json }
                'query_picking_workflow' { $e10Result = $e10Session.QueryPickingWorkflow([string]$e10Request.parameters.filters_json,[bool]$e10Request.parameters.include_detail,[int]$e10Request.parameters.start_row,[int]$e10Request.parameters.max_rows) | ConvertFrom-Json }
                'prepare_picking_batch' { $e10Result = $e10Session.PreparePickingBatch([string]$e10Request.parameters.numbers_json,[string]$e10Request.parameters.options_json) | ConvertFrom-Json }
                'inspect_production_return' { $e10Result = $e10Session.InspectProductionReturn([string]$e10Request.parameters.workorder_no) | ConvertFrom-Json }
                'prepare_production_return' { $e10Result = $e10Session.PrepareManualProductionReturn([string]$e10Request.parameters.workorder_no) | ConvertFrom-Json }
                'commit_operation' { $e10Result = $e10Session.CommitOperation([string]$e10Request.parameters.operation_id,"$PSScriptRoot\receipts") | ConvertFrom-Json }
                'discard_operation' { $e10Result = $e10Session.DiscardOperation([string]$e10Request.parameters.operation_id) | ConvertFrom-Json }
                'operation_status' { $e10Result = $e10Session.OperationStatus([string]$e10Request.parameters.operation_id,"$PSScriptRoot\receipts") | ConvertFrom-Json }
                default { $e10Result = @{ ok=$false; error='Unsupported connection operation.' } }
            }
            $e10Response = @{ id=$e10Request.id; operation=$e10Operation; result=$e10Result }
            [Console]::WriteLine((ConvertTo-Json -InputObject $e10Response -Depth 30 -Compress))
        } catch {
            [Console]::WriteLine((ConvertTo-Json -InputObject @{ id=$e10Request.id; result=@{ ok=$false; error='本机连接组件执行失败。'; error_type=$_.Exception.GetType().FullName } } -Compress))
        }
    }
} catch {
    [Console]::Error.WriteLine('E10 connection worker could not initialize.')
    exit 1
} finally {
    if ($e10Session) { $e10Session.Logout() | Out-Null; $e10Session.Dispose() }
}

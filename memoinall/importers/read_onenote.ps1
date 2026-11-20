# Read the desktop application's open notebooks using the Office object model.
param($Application)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$app = $Application
try {
    if ($null -eq $app) {
        $assembly = [System.Reflection.Assembly]::LoadWithPartialName('Microsoft.Office.Interop.OneNote')
        if ($assembly) {
            $app = New-Object Microsoft.Office.Interop.OneNote.ApplicationClass
        } else {
            $app = New-Object -ComObject OneNote.Application
        }
    }

    [string]$tree = ''
    # hsPages = 4, xs2013 = 2, piBasic = 0 (omit embedded binary data).
    $app.GetHierarchy('', 4, [ref]$tree, 2)
    [xml]$hierarchy = $tree
    $ns = [System.Xml.XmlNamespaceManager]::new($hierarchy.NameTable)
    $ns.AddNamespace('one', $hierarchy.DocumentElement.NamespaceURI)
    $live = "not(ancestor-or-self::*[@isRecycleBin='true' or @isInRecycleBin='true' or @isDeleted='true'])"
    $unlocked = "not(ancestor-or-self::*[@locked='true' or @isLocked='true'])"
    $locked = $hierarchy.SelectNodes("//one:Section[$live][not($unlocked)]", $ns).Count
    $failed = 0
    foreach ($page in $hierarchy.SelectNodes("//one:Page[$live][$unlocked]", $ns)) {
        $id = $page.GetAttribute('ID')
        try {
            [string]$content = ''
            $app.GetPageContent($id, [ref]$content, 0, 2)
            [xml]$doc = $content
            $root = $doc.DocumentElement
            if ($root.LocalName -ne 'Page' -or $root.GetAttribute('ID') -ne $id) {
                throw 'Unexpected page'
            }
            $path = foreach ($ancestor in $page.SelectNodes('ancestor::one:Notebook | ancestor::one:SectionGroup | ancestor::one:Section', $ns)) {
                $ancestor.GetAttribute('name')
            }
            $lines = @(foreach ($t in $root.SelectNodes('one:Outline//one:T', $ns)) { $t.InnerText })
            $titles = @(foreach ($t in $root.SelectNodes('one:Title//one:T', $ns)) { $t.InnerText })
            $row = @{
                id = $id
                title = $titles -join ''
                name = $page.GetAttribute('name')
                lines = $lines
                path = $path -join ' / '
                created = $root.GetAttribute('dateTime')
                updated = $root.GetAttribute('lastModifiedTime')
            }
            [Console]::WriteLine((ConvertTo-Json -InputObject $row -Compress))
        } catch {
            $failed++
        }
    }
    $summary = @{ notebooks = $hierarchy.SelectNodes('//one:Notebook', $ns).Count; locked = $locked; failed = $failed }
    [Console]::WriteLine((ConvertTo-Json -InputObject $summary -Compress))
} catch {
    exit 1
} finally {
    if ($null -eq $Application -and $null -ne $app) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) }
}

// Dán vào script.google.com -> Triển khai dạng Ứng dụng web (Execute as: Me, Who has access: Anyone)
const KEY = 'DAT_KHOA_O_DAY';
const FOLDER_NAME = 'CLAUDE BOT - KIEM DINH';

function doPost(e) {
  if (e.parameter.key !== KEY) return ContentService.createTextOutput('SAI KEY');
  const d = JSON.parse(e.postData.contents);
  const it = DriveApp.getFoldersByName(FOLDER_NAME);
  const folder = it.hasNext() ? it.next() : DriveApp.createFolder(FOLDER_NAME);
  const old = folder.getFilesByName(d.name);
  while (old.hasNext()) old.next().setTrashed(true);
  const blob = Utilities.newBlob(Utilities.base64Decode(d.data), MimeType.MICROSOFT_EXCEL, d.name);
  const f = folder.createFile(blob);
  return ContentService.createTextOutput('OK ' + f.getUrl());
}

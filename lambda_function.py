# -*- coding: utf-8 -*-

import os
import sys
import string
import random
import json
import urllib.parse
import boto3
import re
import smtplib
import email
import base64
from email                import encoders
from email.header         import decode_header
from email.mime.base      import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from email.mime.image     import MIMEImage
from datetime             import datetime

sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'vendored'))
import pyminizip

s3 = boto3.client('s3')

class MailParser(object):
    """
    メール解析クラス
    (参考) http://qiita.com/sayamada/items/a42d344fa343cd80cf86
    """

    def __init__(self, email_string):
        """
        初期化
        """
        self.email_message    = email.message_from_string(email_string)
        self.subject          = None
        self.from_address     = None
        self.reply_to_address = None
        self.body             = ""
        self.attach_file_list = []

        # emlの解釈
        self._parse()

    def get_attr_data(self):
        """
        メールデータの取得
        """
        attr = {
                "from":         self.from_address,
                "reply_to":     self.reply_to_address,
                "subject":      self.subject,
                "body":         self.body,
                "attach_files": self.attach_file_list
                }
        return attr


    def _parse(self):
        """
        メールファイルの解析
        """

        # メッセージヘッダ部分の解析
        self.subject          = self._get_decoded_header("Subject")
        self.from_address     = self._get_decoded_header("From")
        self.reply_to_address = self._get_decoded_header("Reply-To")

        # メールアドレスの文字列だけ抽出する
        from_list =  re.findall(r"<(.*@.*)>", self.from_address)
        if from_list:
            self.from_address = from_list[0]
        reply_to_list =  re.findall(r"<(.*@.*)>", self.reply_to_address)
        if reply_to_list:
            self.reply_to_address = ','.join(reply_to_list)

        # メッセージ本文部分の解析
        for part in self.email_message.walk():
            # ContentTypeがmultipartの場合は実際のコンテンツはさらに
            # 中のpartにあるので読み飛ばす
            if part.get_content_maintype() == 'multipart':
                continue
            # ファイル名の取得
            attach_fname = part.get_filename()
            # ファイル名がない場合は本文のはず
            if not attach_fname:
                charset = str(part.get_content_charset())
                if charset != None:
                    if charset == 'utf-8':
                        self.body += part.get_payload()
                    else:
                        self.body += part.get_payload(decode=True).decode(charset, errors="replace")
                else:
                    self.body += part.get_payload(decode=True)
            else:
                # ファイル名があるならそれは添付ファイルなので
                # データを取得する
                self.attach_file_list.append({
                    "name": attach_fname,
                    "data": part.get_payload(decode=True)
                })

    def _get_decoded_header(self, key_name):
        """
        ヘッダーオブジェクトからデコード済の結果を取得する
        """
        ret = ""

        # 該当項目がないkeyは空文字を戻す
        raw_obj = self.email_message.get(key_name)
        if raw_obj is None:
            return ""
        # デコードした結果をunicodeにする
        for fragment, encoding in decode_header(raw_obj):
            if not hasattr(fragment, "decode"):
                ret += fragment
                continue
            # encodeがなければとりあえずUTF-8でデコードする
            if encoding:
                ret += fragment.decode(encoding)
            else:
                ret += fragment.decode("UTF-8")
        return ret

class MailForwarder(object):

    def __init__(self, email_attr):
        """
        初期化
        """
        self.email_attr = email_attr
        self.encode     = 'utf-8'

    def send(self):
        """
        添付ファイルにパスワード付き圧縮を行い転送、さらにパスワード通知メールを送信
        """

        # パスワード生成
        password = self._generate_password()

        # zipデータ生成
        zip_name = datetime.now().strftime('%Y%m%d%H%M%S')
        zip_data = self._generate_zip(zip_name, password)

        # zipデータを送信
        self._forward_with_zip(zip_name, zip_data)

        # パスワードを送信
        self._send_password(zip_name, password)

    def _generate_password(self):
        """
        パスワード生成
        記号、英字、数字からそれぞれ4文字ずつ取ってシャッフル
        """
        password_chars = ''.join(random.sample(string.punctuation, 4)) + \
                         ''.join(random.sample(string.ascii_letters, 4)) + \
                         ''.join(random.sample(string.digits, 4))

        return ''.join(random.sample(password_chars, len(password_chars)))

    def _generate_zip(self, zip_name, password):
        """
        パスワード付きZipファイルのデータを生成
        """
        tmp_dir  = "/tmp/" + zip_name
        os.mkdir(tmp_dir)

        # 一旦ローカルにファイルを保存
        for attach_file in self.email_attr['attach_files']:
            f = open(tmp_dir + "/" + attach_file['name'], 'wb')
            f.write(attach_file['data'])
            f.flush()
            f.close()

        # パスワード付きzipに
        dst_file_path = "/tmp/%s.zip" % zip_name
        src_file_names = ["%s/%s" % (tmp_dir, name) for name in os.listdir(tmp_dir)]

        pyminizip.compress_multiple(src_file_names, dst_file_path, password, 4)

        # # 生成したzipファイルを読み込み
        r = open(dst_file_path, 'rb')
        zip_data = r.read()
        r.close()

        return zip_data

    def _forward_with_zip(self, zip_name, zip_data):
        """
        パスワード付きZipファイルのデータを生成
        """
        self._send_message(
                self.email_attr['subject'],
                self.email_attr["body"].encode(self.encode),
                zip_name,
                zip_data
                )
        return

    def _send_password(self, zip_name, password):
        """
        zipファイルのパスワードを送信
        """

        subject = self.email_attr['subject']

        message = """
先ほどお送りしたファイルのパスワードのお知らせです。

[件名] {}
[ファイル名] {}.zip
[パスワード] {}
        """.format(subject, zip_name, password)

        self._send_message(
                '[password]%s' % subject,
                message,
                None,
                None
                )
        return

    def _send_message(self, subject, message, attach_name, attach_data):
        """
        メール送信
        """

        msg = MIMEMultipart()

        # ヘッダ
        msg['Subject'] = subject
        msg['From']    = self.email_attr['from']
        msg['To']      = self.email_attr['reply_to']
        msg['Bcc']     = self.email_attr['from']

        # 本文
        body = MIMEText(message, 'plain', self.encode)
        msg.attach(body)

        # 添付ファイル
        if attach_data:
            file_name = "%s.zip" % attach_name
            attachment = MIMEBase('application', 'zip')
            attachment.set_param('name', file_name)
            attachment.set_payload(attach_data)
            encoders.encode_base64(attachment)
            attachment.add_header("Content-Dispositon", "attachment", filename=file_name)
            msg.attach(attachment)

        # 送信
        smtp_server   = self._get_decrypted_environ("SMTP_SERVER")
        smtp_port     = self._get_decrypted_environ("SMTP_PORT")
        smtp_user     = self._get_decrypted_environ("SMTP_USER")
        smtp_password = self._get_decrypted_environ("SMTP_PASSWORD")
        smtp = smtplib.SMTP(smtp_server, smtp_port)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)
        smtp.quit()
        print("Successfully sent email")

        return

    def _get_decrypted_environ(self, key):
        """
        暗号化された環境変数を復号化
        """

        client = boto3.client('kms')
        encrypted_data = os.environ[key]
        return client.decrypt(CiphertextBlob=base64.b64decode(encrypted_data))['Plaintext'].decode('utf-8')

def lambda_handler(event, context):

    # イベントからバケット名、キー名を取得
    bucket = event['Records'][0]['s3']['bucket']['name']
    key = urllib.parse.unquote_plus(event['Records'][0]['s3']['object']['key'])

    try:
        # S3からファイルの中身を読み込む
        s3_object = s3.get_object(Bucket=bucket, Key=key)
        email_string = s3_object['Body'].read().decode('utf-8')

        # メールを解析
        parser = MailParser(email_string)

        # メール転送
        forwarder = MailForwarder(parser.get_attr_data())
        forwarder.send()
        return

    except Exception as e:
        print(e)
        raise e



# -*- coding: utf-8 -*-

##############################################################################
#                                                                            #
#  Copyright (C) 2012 Proge Informática Ltda (<http://www.proge.com.br>).    #
#                                                                            #
#  Author Daniel Hartmann <daniel@proge.com.br>                              #
#                                                                            #
#  This program is free software: you can redistribute it and/or modify      #
#  it under the terms of the GNU Affero General Public License as            #
#  published by the Free Software Foundation, either version 3 of the        #
#  License, or (at your option) any later version.                           #
#                                                                            #
#  This program is distributed in the hope that it will be useful,           #
#  but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#  GNU Affero General Public License for more details.                       #
#                                                                            #
#  You should have received a copy of the GNU Affero General Public License  #
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                            #
##############################################################################

from osv import fields, osv
from tools.translate import _
import base64
import urllib2
import sys
from pysped_nfse.processador import ProcessadorNFSe, SIGNATURE
from pysped_nfse.processador_sp import ProcessadorNFSeSP, tpRPS, tpNFe
from pysped_nfse.nfse_xsd import *
from pysped_nfse.exception import CommunicationError
from uuid import uuid4
import datetime
import re
import unicodedata
import string

NFSE_STATUS = {
    'send_ok': 'Transmitida',
    'send_failed': 'Falhou ao transmitir',
    'cancel_ok': 'Cancelada',
    'cancel_failed': 'Falhou ao cancelar',
    }


class manage_nfse(osv.osv_memory):
    """Manage NFS-e

    States:
    - init: wizard just opened
    - down: server is down
    - done: nfse was successfully sent
    - failed: some or all operations failed
    - nothing: nothing to do
    """

    _name = "l10n_br_nfse.manage_nfse"
    _description = "Manage NFS-e"
    _inherit = "ir.wizard.screen"
    _columns = {
        'company_id': fields.many2one('res.company', 'Company'),
        'state': fields.selection([('init', 'init'),
                                   ('down', 'down'),
                                   ('done', 'done'),
                                   ('failed', 'failed'),
                                   ('nothing', 'nothing'),
                                   ], 'state', readonly=True),
        'selected_invoices': fields.many2many('account.invoice',
                                              string=u'Faturas Selecionadas',
                                              ),
        }
    _defaults = {
        'state': 'init',
        'company_id': lambda self, cr, uid, c: self.pool.get(
            'res.company'
            )._company_default_get(cr, uid, 'account.invoice', context=c),
        }

    def default_get(self, cr, uid, fields, context=None):
        if context is None:
            context = {}

        data = super(manage_nfse, self).default_get(cr, uid, fields, context)
        active_ids = context.get('active_ids', [])

        invoices = self.pool.get('account.invoice').browse(cr, uid, active_ids,
                                                           context=context
                                                           )
        selected_invoices = [i.id for i in invoices
                           if i.state in ('open', 'sefaz_export', 'paid')]
        data.update(selected_invoices=selected_invoices)

        return data

    def _check_server(self, cr, uid, ids, server_host):
        """Check if server is up"""
        server_up = False

        if not server_host.startswith('http'):
            server_host = 'https://' + server_host

        try:
            if urllib2.urlopen(server_host).getcode() == 200:
                server_up = True
        except urllib2.HTTPError:
            server_up = False

        if not server_up:
            raise osv.except_osv(
                u'Erro de comunicação!',
                u'Não foi possível a conexão com o servidor. ' + \
                u'Tente novamente mais tarde.'
                )

        return server_up

    def _check_invoices_are_services(self, invoices):
        check = True
        for inv in invoices:
            if inv.fiscal_type != 'service':
                check = False
                break
        return check

    def _check_certificate(self, company):
        if not company.nfse_cert_file or not company.nfse_cert_password:
            raise osv.except_osv(
                u'Faltam dados no cadastro da empresa.',
                u'Um certificado e sua senha correspondente devem ser ' +
                u'informados na aba NFS-e do cadastro da empresa %s.' %
                company.name,
                )

    def _show_warnings_and_errors(self, invoice_rps, warnings, errors):
        message = ''

        if len(warnings):
            message += u'Alertas:\n'
            for chave in warnings:
                invoice = invoice_rps[str(chave.NumeroRPS)]
                message += u'Nota Fiscal {}:\n'.format(invoice.number) + \
                    '\n'.join(
                        [u'{} - {}'.format(code, desc)
                         for code, desc in warnings[chave]]
                        ) + '\n'

        if len(errors):
            message += u'\nErros:\n'
            for chave in errors:
                invoice = invoice_rps[str(chave.NumeroRPS)]
                message += u'Nota Fiscal {}:\n'.format(invoice.number) + \
                    '\n'.join(
                        [u'{} - {}'.format(code, desc)
                         for code, desc in errors[chave]]
                        ) + '\n'

        raise osv.except_osv(
            u'O sistema da prefeitura verificou problemas nos dados informados',
            message
            )

    def _send_nfse(self, cr, uid, ids, context, test=True):
        """Test NFS-e dispatch"""
        result = {}

        inv_obj = self.pool.get('account.invoice')
        active_ids = [i.id for i in
                      self.browse(cr, uid, ids[0]).selected_invoices]

        if len(active_ids) == 0:
            raise osv.except_osv(
                u'Atenção!',
                u'Não há notas confirmadas para efetuar o envio.'
                )

        conditions = [('id', 'in', active_ids),
                      '|', ('nfe_status', '=', None),
                      ('nfse_status', '!=', NFSE_STATUS['send_ok'])]
        invoices_to_send = inv_obj.search(cr, uid, conditions)

        lote_rps = []
        valor_total_servicos = 0
        valor_total_deducoes = 0
        datas = []

        invoices = inv_obj.browse(cr, uid, invoices_to_send, context=context)
        
        if not self._check_invoices_are_services(invoices):
            raise osv.except_osv(
                u'Não foi possível completar a operação.',
                u'Uma ou mais faturas não são de serviço.',
                )

        invoice_rps = {}

        for inv in invoices:
            company = self.pool.get('res.company').browse(
                cr, uid, inv.company_id.id
                )

            self._check_certificate(company)

            cert_file_content = base64.decodestring(company.nfse_cert_file)

            caminho_temporario = u'/tmp/'
            cert_file = caminho_temporario + uuid4().hex
            arq_tmp = open(cert_file, 'w')
            arq_tmp.write(cert_file_content)
            arq_tmp.close()

            cert_password = company.nfse_cert_password

            company_addr_ids = self.pool.get('res.partner').address_get(cr, uid, [company.partner_id.id], ['default'])
            company_addr = self.pool.get('res.partner.address').browse(cr, uid, [company_addr_ids['default']])[0]
            partner_addr_ids = self.pool.get('res.partner').address_get(cr, uid, [inv.partner_id.id], ['default'])
            partner_addr = self.pool.get('res.partner.address').browse(cr, uid, [partner_addr_ids['default']])[0]

            proc = ProcessadorNFSeSP(
                cert_file,
                cert_password,
                )

            if self._check_server(cr, uid, ids, proc.servidor):

                data_emissao = inv.date_invoice

                if partner_addr.l10n_br_city_id and partner_addr.state_id:
                    city_ibge_code = str(partner_addr.state_id.ibge_code) + \
                        str(partner_addr.l10n_br_city_id.ibge_code)
                else:
                    city_ibge_code = None

                valor_servicos = inv.amount_untaxed
                valor_deducoes = 0
                if inv.amount_tax < 0:
                    valor_deducoes = inv.amount_tax * -1

                valor_total_servicos += valor_servicos
                valor_total_deducoes += valor_deducoes

                impostos = ('pis', 'cofins', 'inss', 'ir', 'csll', 'iss',
                            'iss_retido')
                valores = {x: 0 for x in impostos}
                aliquota = 0

                for inv_tax in inv.tax_line:
                    if inv_tax.tax_code_id.domain in impostos:
                        valores[inv_tax.tax_code_id.domain] += \
                            round(inv_tax.amount, 2)
                        if inv_tax.tax_code_id.domain == 'iss':
                            aliquota = round(inv_tax.amount, 2)

                iss_retido = valores['iss_retido'] < 0

                discriminacoes = []

                for inv_line in inv.invoice_line:
                    discriminacoes.append(inv_line.name)

                discriminacao = '|'.join(discriminacoes)

                inscricao_municipal_tomador = inv.partner_id.inscr_mun

                # São Paulo
                if company_addr.l10n_br_city_id.ibge_code == '50308':                
                    if partner_addr.l10n_br_city_id.ibge_code == '50308' and \
                        not inscricao_municipal_tomador:
                        raise osv.except_osv(
                            u'Faltam dados no cadastro do tomador.',
                            u'Informe a inscrição municipal do parceiro %s.' %
                            inv.partner_id.name,
                            )
                    elif partner_addr.l10n_br_city_id.ibge_code != '50308':
                        inscricao_municipal_tomador = None

                service_code = inv.fiscal_operation_id.code

                if inv.partner_id.cnpj_cpf:
                    cnpj_tomador = re.sub('[^0-9]', '', inv.partner_id.cnpj_cpf)
                else:
                    raise osv.except_osv(
                        u'Faltam dados no cadastro do cliente.',
                        u'O CNPJ do cliente %s é obrigatório.' %
                        inv.partner_id.name,
                        )

                lote_rps.append({
                    # FIXME: por enquanto somente RPS suportado
                    'TipoRPS': 'RPS',
                    'DataEmissao': data_emissao,
                    # TODO: tpStatusNFe
                    'StatusRPS': 'N',
                    'TributacaoRPS': company.tributacao or 'T',
                    'ValorServicos': valor_servicos,
                    'ValorDeducoes': valor_deducoes,
                    'ValorPIS': valores['pis'],
                    'ValorCOFINS': valores['cofins'],
                    'ValorINSS': valores['inss'],
                    'ValorIR': valores['ir'],
                    'ValorCSLL': valores['csll'],
                    'CodigoServico': int(service_code),
                    'AliquotaServicos': aliquota,
                    'ISSRetido': iss_retido,
                    'CPFCNPJTomador': cnpj_tomador,
                    'TipoInscricaoTomador': inv.partner_id.tipo_pessoa,
                    'InscricaoMunicipalTomador': inscricao_municipal_tomador,
                    'InscricaoEstadualTomador': inv.partner_id.inscr_est or None,
                    'RazaoSocialTomador': inv.partner_id.legal_name,
                    'Logradouro': partner_addr.street,
                    'NumeroEndereco': partner_addr.number,
                    'ComplementoEndereco': partner_addr.street2,
                    'Bairro': partner_addr.district,
                    'Cidade': city_ibge_code,
                    'UF': partner_addr.state_id and \
                        partner_addr.state_id.code or None,
                    'CEP': partner_addr.zip,
                    'EmailTomador': partner_addr.email,
                    'Discriminacao': discriminacao,
                    'SerieRPS': int(inv.document_serie_id.code),
                    'NumeroRPS': inv.internal_number,
                    })

                datas.append(data_emissao)
                invoice_rps[inv.internal_number] = inv

        if len(lote_rps):
            datas.sort()
            if company.cnpj:
                cnpj_remetente = re.sub('[^0-9]', '', company.cnpj)
            else:
                raise osv.except_osv(
                    u'Faltam dados no cadastro da empresa.',
                    u'O CNPJ da empresa %s é obrigatório.' %
                    company.name,
                    )
            cabecalho = {
                'CPFCNPJRemetente': cnpj_remetente,
                'InscricaoMunicipalPrestador': re.sub(
                    '[^0-9]', '', company.inscr_mun
                    ),
                'transacao': True,
                'dtInicio': datas[0],
                'dtFim': datas[-1],
                'QtdRPS': len(lote_rps),
                'ValorTotalServicos': valor_total_servicos,
                'ValorTotalDeducoes': valor_total_deducoes,
                'Versao': 1,
                }

            try:
                if test:
                    success, res, warnings, errors = proc.testar_envio_lote_rps(
                        cabecalho=cabecalho,
                        lote_rps=lote_rps
                        )
                else:
                    success, res, warnings, errors = proc.enviar_lote_rps(
                        cabecalho=cabecalho,
                        lote_rps=lote_rps
                        )
            except CommunicationError, e:
                raise osv.except_osv(
                    u'Ocorreu um erro de comunicação.',
                    u'Código: {}\nDescrição: {}'.format(e.status, e.reason)
                    )

            if len(warnings) == 0 and success and test:
                raise osv.except_osv(
                    u'Aviso',
                    u'Os dados foram validados com sucesso.'
                    )

            elif not test and success:
                chave_nfe_rps = res.ChaveNFeRPS

                for chave in chave_nfe_rps:
                    chave_nfe = chave.ChaveNFe
                    chave_rps = chave.ChaveRPS

                    numero_rps = chave_rps.NumeroRPS
                    invoice = invoice_rps[numero_rps]

                    numero_nfe = chave_nfe.NumeroNFe
                    codigo_ver = chave_nfe.CodigoVerificacao

                    data = {
                        'nfse_status': NFSE_STATUS['send_ok'],
                        'nfse_numero': int(numero_nfe),
                        'nfse_codigo_verificacao': codigo_ver,
                        }
                    inv_obj.write(cr, uid, invoice.id, data, context=context)
                    result = {'state': 'done'}

                    for chave in warnings:
                        for code, warning in warnings[chave]:
                            if code == '208':
                                invoice = invoice_rps[
                                    chave.NumeroRPS
                                    ]
                                data = {'nfse_retorno': warning}
                                inv_obj.write(
                                    cr, uid, invoice.id, data, context=context
                                    )
                                self.write(cr, uid, ids, result)
                                cr.commit()
                                raise osv.except_osv(
                                    u'Alíquotas divergentes!',
                                    u'Para evitar a inconsistência dos ' + \
                                    u'dados no sistema, cancele a NFS-e ' + \
                                    u'(número {}) '.format(invoice.number) + \
                                    u'e corrija a alíquota.\nRetorno do ' + \
                                    u'sistema da prefeitura:\n\n"' + \
                                    warning + '"'
                                    )

                if len(warnings):
                    self._show_warnings_and_errors(
                        invoice_rps, warnings, errors
                        )

            else:
                self._show_warnings_and_errors(invoice_rps, warnings, errors)

        else:
            result = {'state': 'nothing'}

        self.write(cr, uid, ids, result)

        return True

    def test_send_nfse(self, cr, uid, ids, context=None):
        return self._send_nfse(cr, uid, ids, context, True)

    def send_nfse(self, cr, uid, ids, context=None):
        """Send one or many NFS-e"""
        return self._send_nfse(cr, uid, ids, context, False)

    def cancel_nfse(self, cr, uid, ids, context=None):
        """Cancel one or many NFS-e"""

        canceled_invoices = []
        failed_invoices = []

        inv_obj = self.pool.get('account.invoice')
        active_ids = [i.id for i in
                      self.browse(cr, uid, ids[0]).selected_invoices]

        if len(active_ids) == 0:
            raise osv.except_osv(
                u'Atenção!',
                u'Não há notas confirmadas para efetuar o cancelamento.'
                )

        conditions = [('id', 'in', active_ids),
                      ('nfse_status', '=', NFSE_STATUS['send_ok'])]
        invoices_to_cancel = inv_obj.search(cr, uid, conditions)

        if len(invoices_to_cancel) == 0:
            raise osv.except_osv(
                u'Não foi possível cancelar a nota fiscal',
                u'A nota fiscal ainda não foi enviada, portanto não é ' + \
                u'possível cancela-la.'
                )

        for inv in inv_obj.browse(cr, uid, invoices_to_cancel,
                                  context=context):

            if not inv.nfse_numero or not inv.nfse_codigo_verificacao:
                raise osv.except_osv(
                    u'Não foi possível cancelar a nota fiscal',
                    u'A nota fiscal de número {} ainda '.format(inv.number) + \
                    u'não foi enviada, portanto não é possível cancela-la.'
                    )

            company = self.pool.get('res.company').browse(
                cr, uid, inv.company_id.id
                )
            self._check_certificate(company)
            cert_file_content = base64.decodestring(company.nfse_cert_file)

            caminho_temporario = u'/tmp/'
            cert_file = caminho_temporario + uuid4().hex
            arq_tmp = open(cert_file, 'w')
            arq_tmp.write(cert_file_content)
            arq_tmp.close()

            cert_password = company.nfse_cert_password

            processor = ProcessadorNFSeSP(cert_file, cert_password)

            self._check_server(cr, uid, ids, processor.servidor)

            try:
                success, res, warnings, errors = processor.cancelar_nfse({
                    'CPFCNPJRemetente': re.sub('[^0-9]', '', company.cnpj),
                    'InscricaoPrestador': company.inscr_mun,
                    'InscricaoTomador': inv.partner_id.inscr_mun,
                    'NumeroRPS': inv.internal_number,
                    'SerieRPS': inv.document_serie_id.code,
                    'NumeroNFe': inv.nfse_numero,
                    'CodigoVerificacao': inv.nfse_codigo_verificacao,
                    'Versao': 1,
                    })
            except CommunicationError, e:
                raise osv.except_osv(
                    u'Ocorreu um erro de comunicação.',
                    u'Código: {}\nDescrição: {}'.format(e.status, e.reason)
                    )

            if success:
                canceled_invoices.append(inv.id)

                data = {'nfse_status': NFSE_STATUS['cancel_ok']}

            else:
                self._show_warnings_and_errors(warnings, errors)

            self.pool.get('account.invoice').write(
                cr, uid, inv.id, data, context=context
                )

        if len(canceled_invoices) == 0 and len(failed_invoices) == 0:
            result = {'state': 'nothing'}
        elif len(failed_invoices) > 0:
            result = {'state': 'failed'}
        else:
            result = {'state': 'done'}

        self.write(cr, uid, ids, result)

        return True

    def check_nfse(self, cr, uid, ids, context=None):
        """Check one or many NFS-e"""

        inv_obj = self.pool.get('account.invoice')
        active_ids = [i.id for i in
                      self.browse(cr, uid, ids[0]).selected_invoices]

        if len(active_ids) == 0:
            raise osv.except_osv(
                u'Atenção!',
                u'Não há notas confirmadas para efetuar a consulta.'
                )

        conditions = [('id', 'in', active_ids)]
        invoices = inv_obj.search(cr, uid, conditions)

        for inv in inv_obj.browse(cr, uid, invoices, context=context):
            if not inv.nfse_numero or not inv.nfse_codigo_verificacao:
                raise osv.except_osv(
                    u'Não foi possível consultar a nota fiscal',
                    u'A nota fiscal de número {} ainda '.format(inv.number) + \
                    u'não foi enviada, portanto não é possível consulta-la.'
                    )

            company = self.pool.get('res.company').browse(
                cr, uid, inv.company_id.id
                )
            self._check_certificate(company)
            cert_file_content = base64.decodestring(company.nfse_cert_file)

            caminho_temporario = u'/tmp/'
            cert_file = caminho_temporario + uuid4().hex
            arq_tmp = open(cert_file, 'w')
            arq_tmp.write(cert_file_content)
            arq_tmp.close()

            cert_password = company.nfse_cert_password

            processor = ProcessadorNFSeSP(cert_file, cert_password)

            self._check_server(cr, uid, ids, processor.servidor)

            try:
                success, res, warnings, errors = processor.consultar_nfse({
                    'CPFCNPJRemetente': re.sub('[^0-9]', '', company.cnpj),
                    'InscricaoPrestador': company.inscr_mun,
                    'NumeroNFe': inv.nfse_numero,
                    'CodigoVerificacao': inv.nfse_codigo_verificacao,
                    'Versao': 1,
                    })
            except CommunicationError, e:
                raise osv.except_osv(
                    u'Ocorreu um erro de comunicação.',
                    u'Código: {}\nDescrição: {}'.format(e.status, e.reason)
                    )

            if success:
                
                nfe = res.NFe[0]
                if nfe.StatusNFe == 'C':
                    raise osv.except_osv(
                        u'Aviso',
                        u'Nota fiscal consta como cancelada.'
                        )
                elif nfe.StatusNFe == 'E':
                    raise osv.except_osv(
                        u'Aviso',
                        u'Nota fiscal consta como extraviada.'
                        )

            else:
                self._show_warnings_and_errors(warnings, errors)

        return True


manage_nfse()

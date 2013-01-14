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
import urllib
import sys
from pysped_nfse.processador import ProcessadorNFSe, SIGNATURE
from pysped_nfse.processador_sp import ProcessadorNFSeSP, tpRPS
from pysped_nfse.nfse_xsd import *
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
        'invoice_status': fields.many2many('account.invoice',
                                           string='Invoice Status',
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
        data.update(invoice_status=[i.id for i in invoices])

        return data

    def _check_server(self, cr, uid, ids, server_host):
        """Check if server is pinging"""
        server_up = False

        if not server_host.startswith('http'):
            server_host = 'https://' + server_host

        if urllib.urlopen(server_host).getcode() == 200:
            server_up = True

        if not server_up:
            self.write(cr, uid, ids, {'state': 'down'})

        return server_up

    def _check_invoices_are_services(self, invoices):
        check = True
        for inv in invoices:
            if inv.fiscal_type != 'service':
                check = False
                break
        return check

    def _send_nfse(self, cr, uid, ids, context, test=True):
        """Test NFS-e dispatch"""

        inv_obj = self.pool.get('account.invoice')
        active_ids = context.get('active_ids', [])

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

        for inv in invoices:
            company = self.pool.get('res.company').browse(
                cr, uid, inv.company_id.id
                )

            if not company.nfse_cert_file or not company.nfse_cert_password:
                raise osv.except_osv(
                    u'Faltam dados no cadastro da empresa.',
                    u'Um certificado e sua senha correspondente devem ser ' +
                    u'informados na aba NFS-e do cadastro da empresa %s.' %
                    company.name,
                    )

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
            print 'state:', company_addr.l10n_br_city_id.state_id.code

            processor = ProcessadorNFSeSP(
                cert_file,
                cert_password,
                )

            if self._check_server(cr, uid, ids, processor.servidor):

                data_emissao = inv.date_invoice

                if partner_addr.l10n_br_city_id and partner_addr.state_id:
                    city_ibge_code = str(partner_addr.state_id.ibge_code) + \
                        str(partner_addr.l10n_br_city_id.ibge_code)
                else:
                    city_ibge_code = None

                valor_servicos = inv.amount_untaxed
                valor_deducoes = 0
                if inv.amount_tax < 0:
                    valor_deducoes = inv.amount_tax

                valor_total_servicos += valor_servicos
                valor_total_deducoes += valor_deducoes

                impostos = ('pis', 'cofins','inss','ir','csll','iss_retido')
                valores = {x: 0 for x in impostos}
                aliquota = 0

                for inv_tax in inv.tax_line:
                    if inv_tax.tax_code_id.domain in impostos:
                        valores[inv_tax.tax_code_id.domain] += inv_tax.amount
                        if inv_tax.tax_code_id.domain == 'iss_retido':
                            # FIXME: verificar se esse valor está correto
                            aliquota = inv_tax.tax_code_id.amount

                iss_retido = valores['iss_retido'] < 0

                discriminacoes = []

                for inv_line in inv.invoice_line:
                    discriminacoes.append(inv_line.name)

                discriminacao = '|'.join(discriminacoes)
                
                if not inv.partner_id.inscr_mun:
                    raise osv.except_osv(
                        u'Faltam dados no cadastro do tomador.',
                        u'Informe a inscrição municipal do parceiro %s.' %
                        inv.partner_id.name,
                        )
                if not inv.partner_id.inscr_est:
                    raise osv.except_osv(
                        u'Faltam dados no cadastro do tomador.',
                        u'Informe a inscrição estadual do parceiro %s.' %
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
                    # TODO: tpCodigoServico - cfop
                    'CodigoServico': 12345,
                    'AliquotaServicos': aliquota,
                    'ISSRetido': iss_retido,
                    'CPFCNPJTomador': re.sub('[^0-9]', '', inv.partner_id.cnpj_cpf),
                    'InscricaoMunicipalTomador': inv.partner_id.inscr_mun,
                    'InscricaoEstadualTomador': inv.partner_id.inscr_est,
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
                    'SerieRPS': inv.document_serie_id.code,
                    'NumeroRPS': inv.internal_number,
                    })

                datas.append(data_emissao)

        if len(lote_rps):
            datas.sort()
            cabecalho = {
                'CPFCNPJRemetente': re.sub('[^0-9]', '', company.cnpj),
                'transacao': True,
                'dtInicio': datas[0],
                'dtFim': datas[-1],
                'QtdRPS': len(lote_rps),
                'ValorTotalServicos': valor_total_servicos,
                'ValorTotalDeducoes': valor_total_deducoes,
                'Versao': 1,
                }

            if test:
                code, title, content = processor.testar_envio_lote_rps(
                    cabecalho=cabecalho,
                    lote_rps=lote_rps
                    )
            else:
                code, title, content = processor.enviar_lote_rps(
                    cabecalho=cabecalho,
                    lote_rps=lote_rps
                    )

            print code, title, content

            # FIXME: check result instead of code
            if code == 200:
                data = {'nfse_status': NFSE_STATUS['send_ok']}
                result = {'state': 'done'}
            else:
                reason = '{} - {}'.format(code, title)
                data = {
                    'nfse_status': NFSE_STATUS['send_failed'],
                    'nfse_retorno': reason,
                    }
                result = {'state': 'failed'}

            self.pool.get('account.invoice').write(
                cr, uid, inv.id, data, context=context
                )
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
        active_ids = context.get('active_ids', [])

        conditions = [('id', 'in', active_ids),
                      ('nfse_status', '=', NFSE_STATUS['send_ok'])]
        invoices_to_cancel = inv_obj.search(cr, uid, conditions)

        for inv in inv_obj.browse(cr, uid, invoices_to_cancel,
                                  context=context):
            company = self.pool.get('res.company').browse(cr,
                                                          uid,
                                                          [inv.company_id.id]
                                                          )[0]
            server_host = company.nfse_server_host

            if self._check_server(cr, uid, ids, server_host):
                cert_file_content = base64.decodestring(company.nfse_cert_file)

                caminho_temporario = u'/tmp/'
                cert_file = caminho_temporario + uuid4().hex
                arq_tmp = open(cert_file, 'w')
                arq_tmp.write(cert_file_content)
                arq_tmp.close()

                cert_password = company.nfse_cert_password

                processor = ProcessadorNFSeSP(cert_file, cert_password)

                code, title, content = processor.cancelar_nfse({
                    'CPFCNPJRemetente': company.cnpj,
                    'InscricaoPrestador': company.insc_mun,
                    'InscricaoTomador': inv.partner_id.insc_mun,
                    'NumeroRPS': inv.internal_number,
                    'SerieRPS': inv.document_serie_id.code,
                    # TODO: número gerado pelo sistema da prefeitura
                    'NumeroNFe': '',
                    # TODO: código gerado pelo sistema da prefeitura
                    'CodigoVerificacao': 1,
                    'Versao': 1,
                    })

                print code, title, content

                # FIXME: check result instead of code
                if code == 200:
                    canceled_invoices.append(inv.id)

                    data = {'nfse_status': NFSE_STATUS['cancel_ok']}

                else:
                    failed_invoices.append(inv.id)

                    reason = '{} - {}'.format(code, title)
                    data = {
                        'nfse_status': NFSE_STATUS['cancel_failed'],
                        'nfse_retorno': reason,
                        }

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
        result = {'state': 'failed'}

        inv_obj = self.pool.get('account.invoice')
        active_ids = context.get('active_ids', [])

        conditions = [('id', 'in', active_ids),
                      ('nfse_status', '=', NFSE_STATUS['send_ok'])]
        invoices = inv_obj.search(cr, uid, conditions)

        for inv in inv_obj.browse(cr, uid, invoices,
                                  context=context):
            company = self.pool.get('res.company').browse(cr,
                                                          uid,
                                                          [inv.company_id.id]
                                                          )[0]
            server_host = company.nfse_server_host

            if self._check_server(cr, uid, ids, server_host):
                cert_file_content = base64.decodestring(company.nfse_cert_file)

                caminho_temporario = u'/tmp/'
                cert_file = caminho_temporario + uuid4().hex
                arq_tmp = open(cert_file, 'w')
                arq_tmp.write(cert_file_content)
                arq_tmp.close()

                cert_password = company.nfse_cert_password

                processor = ProcessadorNFSeSP(cert_file, cert_password)

                code, title, content = processor.consultar_nfse({
                    'CPFCNPJRemetente': company.cnpj,
                    'InscricaoPrestador': company.insc_mun,
                    'NumeroRPS': inv.internal_number,
                    'SerieRPS': inv.document_serie_id.code,
                    # TODO: número gerado pelo sistema da prefeitura
                    'NumeroNFe': '',
                    # TODO: código gerado pelo sistema da prefeitura
                    'CodigoVerificacao': 1,
                    'Versao': 1,
                    })

                print code, title, content

                # FIXME: check result instead of code
                if code == 200:
                    data = {'nfse_status': NFSE_STATUS['cancel_ok']}
                    result = {'state': 'done'}

                else:
                    reason = '{} - {}'.format(code, title)
                    data = {
                        'nfse_status': NFSE_STATUS['cancel_failed'],
                        'nfse_retorno': reason,
                        }
                    result = {'state': 'failed'}

                self.pool.get('account.invoice').write(
                    cr, uid, inv.id, data, context=context
                    )

        self.write(cr, uid, ids, result)

        return True


manage_nfse()

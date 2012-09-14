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
from nfse.processador import ProcessadorNFSe, SIGNATURE
from nfse.nfse_xsd import *
from uuid import uuid4
import datetime

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

    def check_server(self, cr, uid, ids, server_host):
        """Check if server is pinging"""
        server_up = False

        if not server_host.startswith('http'):
            server_host = 'https://' + server_host

        if urllib.urlopen(server_host).getcode() == 200:
            server_up = True

        if not server_up:
            self.write(cr, uid, ids, {'state': 'down'})

        return server_up

    def send_nfse(self, cr, uid, ids, context=None):
        """Send one or many NFS-e"""

        sent_invoices = []
        unsent_invoices = []

        inv_obj = self.pool.get('account.invoice')
        active_ids = context.get('active_ids', [])

        conditions = [('id', 'in', active_ids),
                      ('nfse_status', '<>', NFSE_STATUS['send_ok'])]
        invoices_to_send = inv_obj.search(cr, uid, conditions)

        for inv in inv_obj.browse(cr, uid, invoices_to_send, context=context):
            company = self.pool.get('res.company').browse(cr,
                                                          uid,
                                                          [inv.company_id.id]
                                                          )[0]
            server_host = company.nfse_server_host

            if self.check_server(cr, uid, ids, server_host):
                server_address = company.nfse_server_address
                cert_file_content = base64.decodestring(company.nfse_cert_file)

                caminho_temporario = u'/tmp/'
                cert_file = caminho_temporario + uuid4().hex
                arq_tmp = open(cert_file, 'w')
                arq_tmp.write(cert_file_content)
                arq_tmp.close()

                cert_password = company.nfse_cert_password

                processor = ProcessadorNFSe(
                    server_host,
                    server_address,
                    cert_file,
                    cert_password,
                    )

                id_rps = tcIdentificacaoRps(Numero=1, Serie=1, Tipo=1)
                data_emissao = datetime.datetime(2012, 2, 13).isoformat()
                prestador = tcIdentificacaoPrestador(Cnpj='22222222000191')
                inf_rps = tcInfRps(
                    IdentificacaoRps=id_rps,
                    DataEmissao=data_emissao,
                    NaturezaOperacao=1,
                    RegimeEspecialTributacao=1,
                    OptanteSimplesNacional=True,
                    IncentivadorCultural=True,
                    Status=1,
                    Servico=tcDadosServico(Valores=tcValores(ValorServicos=1,
                                                             ValorDeducoes=1,
                                                             ValorPis=1,
                                                             ValorCofins=1,
                                                             ValorInss=1,
                                                             ValorIr=1,
                                                             ValorCsll=1,
                                                             IssRetido=1,
                                                             ),
                                           ItemListaServico=1,
                                           CodigoCnae=1,
                                           CodigoTributacaoMunicipio=1,
                                           Discriminacao=1,
                                           CodigoMunicipio=1,
                                           ),
                    Prestador=prestador
                    )
                rps = [tcRps(InfRps=inf_rps, Signature=SIGNATURE)]

                lote_rps = tcLoteRps(NumeroLote=1,
                                     Cnpj='22222222000191',
                                     InscricaoMunicipal=1,
                                     QuantidadeRps=1,
                                     ListaRps=ListaRpsType(rps)
                                     )
                code, title, content = processor.enviar_lote_rps(lote_rps)

                # FIXME: check result instead of code
                if code == 200:
                    sent_invoices.append(inv.id)

                    data = {'nfse_status': NFSE_STATUS['send_ok']}
                else:
                    unsent_invoices.append(inv.id)

                    reason = '{} - {}'.format(code, title)
                    data = {
                        'nfse_status': NFSE_STATUS['send_failed'],
                        'nfse_retorno': reason,
                        }

                self.pool.get('account.invoice').write(cr,
                                                       uid,
                                                       inv.id,
                                                       data,
                                                       context=context
                                                       )

        if len(sent_invoices) == 0 and len(unsent_invoices) == 0:
            result = {'state': 'nothing'}
        elif len(unsent_invoices) > 0:
            result = {'state': 'failed'}
        else:
            result = {'state': 'done'}

        self.write(cr, uid, ids, result)

        return True

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

            if self.check_server(cr, uid, ids, server_host):
                server_address = company.nfse_server_address
                cert_file_content = base64.decodestring(company.nfse_cert_file)

                caminho_temporario = u'/tmp/'
                cert_file = caminho_temporario + uuid4().hex
                arq_tmp = open(cert_file, 'w')
                arq_tmp.write(cert_file_content)
                arq_tmp.close()

                cert_password = company.nfse_cert_password

                processor = ProcessadorNFSe(
                    server_host,
                    server_address,
                    cert_file,
                    cert_password,
                    )

                nfse = tcIdentificacaoNfse(Numero=1,
                                           Cnpj='22222222000191',
                                           InscricaoMunicipal=1,
                                           CodigoMunicipio=1
                                           )
                cancelamento = tcInfPedidoCancelamento(IdentificacaoNfse=nfse,
                                                       CodigoCancelamento='E64'
                                                       )

                pedido = tcPedidoCancelamento(
                    InfPedidoCancelamento=cancelamento,
                    Signature=SIGNATURE
                    )

                code, title, content = processor.cancelar_nfse(pedido)

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

                self.pool.get('account.invoice').write(cr,
                                                       uid,
                                                       inv.id,
                                                       data,
                                                       context=context
                                                       )

        if len(canceled_invoices) == 0 and len(failed_invoices) == 0:
            result = {'state': 'nothing'}
        elif len(failed_invoices) > 0:
            result = {'state': 'failed'}
        else:
            result = {'state': 'done'}

        self.write(cr, uid, ids, result)

        return True


manage_nfse()

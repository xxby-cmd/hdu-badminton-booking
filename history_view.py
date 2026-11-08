"""Local plans and read-only school orders. Deletion never calls school cancellation."""
from datetime import datetime,time,date
import threading
import tkinter as tk
from tkinter import ttk,messagebox
from booking import Api,BookingError,TZ,order_date,order_is_cancelled
from session import read_live_session,validate_session
from plan_history import LABELS,spans

class HistoryWindow:
    def __init__(self,panel):
        self.panel=panel;self.store=panel.history;self.school={};self.local={}
        self.root=tk.Toplevel(panel.root);self.root.title('预约历史与待执行计划');self.root.geometry('1060x620')
        self.root.minsize(900,500)
        intro=ttk.Label(self.root,text='本地待执行计划可以删除、覆盖或改场地。学校确认的订单单独显示，隐藏记录不会取消预约。',wraplength=1000)
        intro.pack(fill='x',padx=18,pady=14)
        columns=('source','day','time','venue','site','status')
        self.tree=ttk.Treeview(self.root,columns=columns,show='headings',selectmode='browse')
        widths=(95,110,190,160,100,155)
        for name,label,width in zip(columns,('类型','预约日期','时段','场馆','场地','状态'),widths):
            self.tree.heading(name,text=label);self.tree.column(name,width=width,anchor='w')
        self.tree.pack(fill='both',expand=True,padx=18)
        self.message=ttk.Label(self.root,text='学校预约只显示接口当前返回的记录，非完整取消流水。',wraplength=1000)
        self.message.pack(fill='x',padx=18,pady=10)
        buttons=ttk.Frame(self.root);buttons.pack(fill='x',padx=18,pady=(0,16))
        ttk.Button(buttons,text='刷新历史',command=self.refresh).pack(side='left')
        ttk.Button(buttons,text='删除选中记录',command=self.delete).pack(side='left',padx=8)
        ttk.Button(buttons,text='更改所选计划场地',command=self.edit_site).pack(side='left')
        ttk.Button(buttons,text='到钉钉查看学校预约',command=panel.open_login).pack(side='right')
        self.tree.bind('<<TreeviewSelect>>',self.describe)
        self.refresh()
    def local_rows(self):
        for item in self.tree.get_children():
            if item.startswith('local:'):self.tree.delete(item)
        self.local={}
        for row in sorted(self.store.records(),key=lambda r:r.get('updated_at',''),reverse=True):
            data=row['data'];rid=row['id'];self.local[rid]=row
            times='、'.join(a+'–'+b for a,b in spans(data))
            if data.get('schema')==2:
                sites='、'.join('任意' if not s['site_id'] else str(s['site_id'])+'号' for s in data['selections'])
            else:sites=','.join(map(str,data.get('site_ids',[]))) or '任意'
            self.tree.insert('', 'end',iid='local:'+rid,values=('本地计划',data['day'],times,data['venue'],sites,LABELS.get(row['status'],row['status'])))
    def refresh(self):
        try:
            self.panel.sync_history();self.local_rows()
        except (BookingError,OSError):self.message.configure(text='本地记录暂时无法读取，请保留历史文件并稍后重试')
        self.message.configure(text='正在只读读取学校本人预约…')
        def read():
            try:
                credentials,_=validate_session(read_live_session())
                class ReadOnly(Api):
                    def call(self,endpoint,*a,**kw):
                        if endpoint!='post_order_info':raise AssertionError('No school cancellation here')
                        return super().call(endpoint,*a,**kw)
                api=ReadOnly(credentials)
                try:rows=api.orders(False)
                finally:
                    if api.connection:api.connection.close()
                self.panel.messages.put(('school_history',self,rows))
            except Exception:self.panel.messages.put(('school_history_error',self))
        threading.Thread(target=read,daemon=True).start()
    def show_school(self,rows):
        if not self.root.winfo_exists():return
        for item in self.tree.get_children():
            if item.startswith('school:'):self.tree.delete(item)
        self.school={};hidden=self.store.school_hidden()
        for index,row in enumerate(rows):
            number=str(row.get('order_num',''))
            if not number or number in hidden:continue
            key='school:'+str(index);self.school[key]=row
            state='已取消' if order_is_cancelled(row) else '待邀请同伴' if not row.get('with_friends') else '已邀请同伴'
            try:
                ended=datetime.combine(date.fromisoformat(order_date(row)),time.fromisoformat(row['end_time']),TZ)<datetime.now(TZ)
                if ended and not order_is_cancelled(row):state='已结束'
            except (ValueError,KeyError,TypeError):pass
            self.tree.insert('','end',iid=key,values=('学校订单',order_date(row),str(row.get('start_time',''))+'–'+str(row.get('end_time','')),
                                  row.get('venue_name',''),str(row.get('site_id','')),state))
        self.message.configure(text='学校接口返回 '+str(len(rows))+' 条本人预约。删除学校记录仅隐藏显示，不取消、不释放时段。')
    def selected(self):
        selected=self.tree.selection()
        return selected[0] if selected else None
    def describe(self,event=None):
        key=self.selected()
        if key and key.startswith('local:'):
            row=self.local[key.split(':',1)[1]]
            self.message.configure(text=row.get('message') or '待执行本地计划。删除后释放预选时段；覆盖和改场地会更新同一系统任务。')
    def delete(self):
        key=self.selected()
        if not key:return
        try:
            if key.startswith('local:'):
                rid=key.split(':',1)[1]
                if not messagebox.askyesno('删除本地计划','删除后会停用对应待执行任务并释放本地选格。不会取消学校订单。继续？',parent=self.root):return
                self.panel.delete_history_plan(rid);self.local_rows()
            else:
                row=self.school[key]
                if not messagebox.askyesno('仅隐藏学校记录','这不会取消学校预约，也不会释放对应时段。确认仅从本机历史列表隐藏？',parent=self.root):return
                self.store.hide_school(row['order_num']);self.tree.delete(key)
        except (BookingError,OSError) as exc:messagebox.showerror('未能删除',str(exc),parent=self.root)
    def edit_site(self):
        key=self.selected()
        if not key or not key.startswith('local:'):
            messagebox.showinfo('请选择本地计划','学校订单不能靠改本地记录换场地，请到钉钉处理。',parent=self.root);return
        rid=key.split(':',1)[1];row=self.local[rid]
        if row['status']!='scheduled':
            messagebox.showinfo('暂不能改场地','仅可直接修改尚未启动的定时计划。运行中请先停止并核对。',parent=self.root);return
        editor=tk.Toplevel(self.root);editor.title('更改待执行计划场地');editor.geometry('420x300')
        data=row['data'];model=self.panel.models.get((data['venue'],data['day']))
        if not model:
            editor.destroy();messagebox.showinfo('请先读取排期','先在主窗口选择该场馆并刷新七天排期，再更改场地。',parent=self.root);return
        from plan_history import slots_for
        cells=slots_for(data,model.meta);variables=[]
        ttk.Label(editor,text='只修改尚未执行的本地计划，不操作学校订单。',wraplength=380).pack(padx=16,pady=12)
        sites=['任意场地']+[str(r['site_id']) for r in model.stock['site_situation']]
        for cell in cells:
            line=ttk.Frame(editor);line.pack(fill='x',padx=16,pady=7)
            ttk.Label(line,text=cell.start+'–'+cell.end).pack(side='left')
            var=tk.StringVar(value=str(cell.site_id) if cell.site_id else '任意场地');variables.append(var)
            ttk.Combobox(line,textvariable=var,values=sites,state='readonly',width=12).pack(side='right')
        def save():
            try:
                self.panel.change_history_sites(rid,[0 if v.get()=='任意场地' else int(v.get()) for v in variables])
                editor.destroy();self.local_rows()
            except (BookingError,OSError,ValueError) as exc:messagebox.showerror('不能更改',str(exc),parent=editor)
        ttk.Button(editor,text='确认更改场地',command=save).pack(pady=14)
